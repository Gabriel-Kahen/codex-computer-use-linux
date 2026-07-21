use super::write_kwin_script_file;
use super::KWIN_SCRIPTING_INTERFACE;
use super::KWIN_SCRIPTING_OBJECT_PATH;
use super::KWIN_SCRIPTING_SERVICE;
use crate::diagnostics::hydrate_session_bus_env;
use anyhow::bail;
use anyhow::Context;
use anyhow::Result;
use std::fs;
use std::sync::Arc;
use std::sync::Mutex as StdMutex;
use std::time::Duration;
use std::time::SystemTime;
use std::time::UNIX_EPOCH;
use tokio::sync::Mutex;
use tokio::sync::Notify;
use tokio::time::timeout;
use zbus::Connection;
use zbus::Proxy;

const BRIDGE_CALLBACK_OBJECT_PATH_PREFIX: &str = "/dev/avifenesh/ComputerUseLinux/KWinWindowBridge";
const BRIDGE_CALLBACK_INTERFACE: &str = "dev.avifenesh.ComputerUseLinux.KWinWindowBridge";
const BRIDGE_START_TIMEOUT: Duration = Duration::from_secs(2);

static BRIDGE: Mutex<Option<KwinBridge>> = Mutex::const_new(None);

pub(super) async fn window_snapshot() -> Result<String> {
    hydrate_session_bus_env();

    let mut bridge = BRIDGE.lock().await;
    let owner_changed = match bridge.as_ref() {
        Some(current) => !current.owner_is_current().await,
        None => false,
    };
    if owner_changed {
        if let Some(stale) = bridge.take() {
            stale.discard().await;
        }
    }

    if bridge.is_none() {
        *bridge = Some(KwinBridge::start().await?);
    }

    let current = bridge
        .as_ref()
        .context("KWin bridge was not available after initialization")?;
    let snapshot = current.snapshot()?;
    if !current.owner_is_current().await {
        let stale = bridge.take().context("KWin bridge disappeared")?;
        stale.discard().await;
        bail!("org.kde.KWin changed owners while reading the window snapshot; retry");
    }
    Ok(snapshot)
}

pub(super) async fn shutdown() {
    if let Some(bridge) = BRIDGE.lock().await.take() {
        bridge.cleanup().await;
    }
}

struct KwinBridge {
    connection: Connection,
    kwin_owner: String,
    plugin_name: String,
    callback_object_path: String,
    script_path: std::path::PathBuf,
    state: Arc<BridgeState>,
}

impl KwinBridge {
    async fn start() -> Result<Self> {
        let connection = Connection::session()
            .await
            .context("failed to connect to session bus for persistent KWin bridge")?;
        let kwin_owner = service_owner(&connection, KWIN_SCRIPTING_SERVICE)
            .await
            .context("failed to resolve org.kde.KWin owner before loading bridge")?;
        let service_name = connection
            .unique_name()
            .context("session bus did not assign the KWin bridge a unique name")?
            .to_string();
        let plugin_name = persistent_plugin_name();
        let callback_object_path = format!("{BRIDGE_CALLBACK_OBJECT_PATH_PREFIX}/{plugin_name}");
        let state = Arc::new(BridgeState::default());
        connection
            .object_server()
            .at(
                callback_object_path.as_str(),
                KwinBridgeCallback {
                    state: Arc::clone(&state),
                },
            )
            .await
            .context("failed to register persistent KWin bridge callback")?;

        let source = script_source(
            &service_name,
            &callback_object_path,
            BRIDGE_CALLBACK_INTERFACE,
            &plugin_name,
        )?;
        let script_path = match write_kwin_script_file(&plugin_name, &source) {
            Ok(path) => path,
            Err(error) => {
                let _: Result<bool, _> = connection
                    .object_server()
                    .remove::<KwinBridgeCallback, _>(callback_object_path.as_str())
                    .await;
                return Err(error);
            }
        };

        let start_result = async {
            let scripting_proxy = Proxy::new(
                &connection,
                KWIN_SCRIPTING_SERVICE,
                KWIN_SCRIPTING_OBJECT_PATH,
                KWIN_SCRIPTING_INTERFACE,
            )
            .await
            .context("failed to create KWin scripting proxy for persistent bridge")?;
            let _script_id: i32 = scripting_proxy
                .call(
                    "loadScript",
                    &(script_path.to_string_lossy().as_ref(), plugin_name.as_str()),
                )
                .await
                .context("KWin loadScript failed for persistent bridge")?;
            let _: () = scripting_proxy
                .call("start", &())
                .await
                .context("KWin start failed for persistent bridge")?;

            timeout(BRIDGE_START_TIMEOUT, state.wait_for_snapshot())
                .await
                .context("persistent KWin bridge did not publish an initial snapshot")?;
            let owner_after = service_owner(&connection, KWIN_SCRIPTING_SERVICE)
                .await
                .context("failed to recheck org.kde.KWin owner after loading bridge")?;
            if owner_after != kwin_owner {
                bail!("org.kde.KWin changed owners while loading the persistent bridge");
            }
            // KWin has loaded the source into its script engine; it does not
            // need the file again for updates or unloading.
            fs::remove_file(&script_path).with_context(|| {
                format!(
                    "failed to remove loaded KWin bridge source {}",
                    script_path.display()
                )
            })?;
            Ok(())
        }
        .await;

        if let Err(error) = start_result {
            if service_owner(&connection, KWIN_SCRIPTING_SERVICE)
                .await
                .is_ok_and(|owner| owner == kwin_owner)
            {
                unload_script(&connection, &plugin_name).await;
            }
            let _: Result<bool, _> = connection
                .object_server()
                .remove::<KwinBridgeCallback, _>(callback_object_path.as_str())
                .await;
            let _ = fs::remove_file(&script_path);
            return Err(error);
        }

        Ok(Self {
            connection,
            kwin_owner,
            plugin_name,
            callback_object_path,
            script_path,
            state,
        })
    }

    fn snapshot(&self) -> Result<String> {
        self.state
            .snapshot
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .clone()
            .context("persistent KWin bridge has not published a window snapshot")
    }

    async fn owner_is_current(&self) -> bool {
        service_owner(&self.connection, KWIN_SCRIPTING_SERVICE)
            .await
            .is_ok_and(|owner| owner == self.kwin_owner)
    }

    async fn cleanup(self) {
        if self.owner_is_current().await {
            unload_script(&self.connection, &self.plugin_name).await;
        }
        self.remove_local_state().await;
    }

    async fn discard(self) {
        self.remove_local_state().await;
    }

    async fn remove_local_state(self) {
        let _: Result<bool, _> = self
            .connection
            .object_server()
            .remove::<KwinBridgeCallback, _>(self.callback_object_path.as_str())
            .await;
        let _ = fs::remove_file(self.script_path);
    }
}

#[derive(Default)]
struct BridgeState {
    snapshot: StdMutex<Option<String>>,
    updated: Notify,
}

impl BridgeState {
    async fn wait_for_snapshot(&self) {
        loop {
            let updated = self.updated.notified();
            if self
                .snapshot
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .is_some()
            {
                return;
            }
            updated.await;
        }
    }
}

struct KwinBridgeCallback {
    state: Arc<BridgeState>,
}

#[zbus::interface(name = "dev.avifenesh.ComputerUseLinux.KWinWindowBridge")]
impl KwinBridgeCallback {
    fn receive_windows(&self, json: &str) {
        *self
            .state
            .snapshot
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(json.to_string());
        self.state.updated.notify_waiters();
    }
}

async fn service_owner(connection: &Connection, service: &str) -> Result<String> {
    let proxy = Proxy::new(
        connection,
        "org.freedesktop.DBus",
        "/org/freedesktop/DBus",
        "org.freedesktop.DBus",
    )
    .await
    .context("failed to create session-bus ownership proxy")?;
    proxy
        .call("GetNameOwner", &(service))
        .await
        .with_context(|| format!("session bus has no owner for {service}"))
}

async fn unload_script(connection: &Connection, plugin_name: &str) {
    if let Ok(proxy) = Proxy::new(
        connection,
        KWIN_SCRIPTING_SERVICE,
        KWIN_SCRIPTING_OBJECT_PATH,
        KWIN_SCRIPTING_INTERFACE,
    )
    .await
    {
        let _: Result<bool, _> = proxy.call("unloadScript", &(plugin_name)).await;
    }
}

fn persistent_plugin_name() -> String {
    let pid = std::process::id();
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or_default();
    format!("computer_use_linux_kwin_window_bridge_{pid}_{nanos}")
}

pub(super) fn script_source(
    service_name: &str,
    callback_object_path: &str,
    callback_interface: &str,
    plugin_name: &str,
) -> Result<String> {
    let service_name = serde_json::to_string(service_name)?;
    let object_path = serde_json::to_string(callback_object_path)?;
    let interface = serde_json::to_string(callback_interface)?;
    let plugin_name = serde_json::to_string(plugin_name)?;
    Ok(format!(
        r#"(function() {{
    var serviceName = {service_name};
    var objectPath = {object_path};
    var iface = {interface};
    var pluginName = {plugin_name};
    var watchedWindows = {{}};

    function serialize(value) {{
        if (value === null || value === undefined) {{
            return null;
        }}
        if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {{
            return value;
        }}
        if (Array.isArray(value)) {{
            return value.map(serialize);
        }}
        try {{
            if (typeof value.toString === "function") {{
                return value.toString();
            }}
        }} catch (error) {{}}
        return null;
    }}

    function read(obj, key) {{
        try {{
            if (obj === null || obj === undefined) {{
                return null;
            }}
            var value = obj[key];
            if (typeof value === "function") {{
                return null;
            }}
            return serialize(value);
        }} catch (error) {{
            return null;
        }}
    }}

    function normalizeUuid(value) {{
        var text = serialize(value);
        if (text === null || text === undefined) {{
            return null;
        }}
        text = String(text).trim().toLowerCase();
        if (text.charAt(0) === "{{" && text.charAt(text.length - 1) === "}}") {{
            text = text.substring(1, text.length - 1);
        }}
        return text.length > 0 ? text : null;
    }}

    function windowUuid(window) {{
        return normalizeUuid(read(window, "uuid")) || normalizeUuid(read(window, "internalId"));
    }}

    function geometry(window) {{
        var frame = null;
        try {{
            frame = window.frameGeometry;
        }} catch (error) {{}}
        var x = read(window, "x");
        var y = read(window, "y");
        var width = read(window, "width");
        var height = read(window, "height");
        return {{
            x: x !== null ? x : read(frame, "x"),
            y: y !== null ? y : read(frame, "y"),
            width: width !== null ? width : read(frame, "width"),
            height: height !== null ? height : read(frame, "height")
        }};
    }}

    function firstDesktop(window) {{
        var desktops = read(window, "desktops");
        if (!Array.isArray(desktops) || desktops.length === 0) {{
            return null;
        }}
        var parsed = parseInt(desktops[0], 10);
        return isFinite(parsed) ? parsed : null;
    }}

    function clientType(window) {{
        if (read(window, "waylandClient")) {{
            return "wayland";
        }}
        if (read(window, "x11Client")) {{
            return "x11";
        }}
        return null;
    }}

    function listWindows() {{
        try {{
            if (typeof workspace.windowList === "function") {{
                return workspace.windowList();
            }}
        }} catch (error) {{}}
        try {{
            if (typeof workspace.clientList === "function") {{
                return workspace.clientList();
            }}
        }} catch (error) {{}}
        try {{
            if (workspace.stackingOrder && typeof workspace.stackingOrder.length === "number") {{
                return workspace.stackingOrder;
            }}
        }} catch (error) {{}}
        return [];
    }}

    function connectSignal(object, name, callback) {{
        try {{
            var signal = object[name];
            if (signal && typeof signal.connect === "function") {{
                signal.connect(callback);
            }}
        }} catch (error) {{}}
    }}

    function watchWindow(window) {{
        var uuid = windowUuid(window);
        if (!uuid || watchedWindows[uuid]) {{
            return;
        }}
        watchedWindows[uuid] = true;
        [
            "captionChanged", "desktopFileNameChanged", "windowClassChanged",
            "frameGeometryChanged", "xChanged", "yChanged", "widthChanged", "heightChanged",
            "minimizedChanged", "activeChanged", "desktopChanged", "desktopsChanged",
            "skipTaskbarChanged"
        ].forEach(function(name) {{
            connectSignal(window, name, publishWindows);
        }});
    }}

    function publishWindows() {{
        var activeWindow = null;
        try {{
            activeWindow = "activeWindow" in workspace ? workspace.activeWindow : workspace.activeClient;
        }} catch (error) {{}}
        var windows = listWindows();
        windows.forEach(watchWindow);
        var serialized = windows.map(function(window) {{
            var geo = geometry(window);
            var desktopFile = read(window, "desktopFileName");
            return {{
                uuid: read(window, "uuid"),
                internalId: read(window, "internalId"),
                caption: read(window, "caption"),
                desktopFile: desktopFile !== null ? desktopFile : read(window, "desktopFile"),
                resourceClass: read(window, "resourceClass"),
                resourceName: read(window, "resourceName"),
                windowClass: read(window, "windowClass"),
                pid: read(window, "pid"),
                x: geo.x,
                y: geo.y,
                width: geo.width,
                height: geo.height,
                workspace: firstDesktop(window),
                minimized: read(window, "minimized"),
                active: read(window, "active") || window === activeWindow,
                clientType: clientType(window),
                normalWindow: read(window, "normalWindow"),
                desktopWindow: read(window, "desktopWindow"),
                skipTaskbar: read(window, "skipTaskbar"),
                dock: read(window, "dock")
            }};
        }});
        callDBus(serviceName, objectPath, iface, "ReceiveWindows", JSON.stringify({{
            backend: "kwin",
            pluginName: pluginName,
            windows: serialized
        }}));
    }}

    [
        "windowAdded", "windowRemoved", "windowActivated", "activeWindowChanged",
        "clientAdded", "clientRemoved", "clientActivated", "activeClientChanged",
        "currentDesktopChanged", "currentActivityChanged"
    ].forEach(function(name) {{
        connectSignal(workspace, name, publishWindows);
    }});
    publishWindows();
}})();
"#
    ))
}
