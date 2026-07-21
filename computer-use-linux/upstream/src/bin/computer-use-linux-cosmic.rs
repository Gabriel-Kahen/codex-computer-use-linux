use anyhow::{anyhow, bail, Context, Result};
use computer_use_linux::cosmic_helper_protocol::{
    read_cosmic_service_message, CosmicServiceCommand, CosmicServiceRequest, CosmicServiceResponse,
    COSMIC_SERVICE_PROTOCOL_VERSION,
};
use cosmic_protocols::{
    toplevel_info::v1::client::{zcosmic_toplevel_handle_v1, zcosmic_toplevel_info_v1},
    toplevel_management::v1::client::zcosmic_toplevel_manager_v1,
};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::io::{self, Write};
use wayland_client::{
    event_created_child,
    globals::{registry_queue_init, GlobalListContents},
    protocol::{wl_registry, wl_seat},
    Connection, Dispatch, Proxy, QueueHandle, WEnum,
};
use wayland_protocols::ext::foreign_toplevel_list::v1::client::{
    ext_foreign_toplevel_handle_v1, ext_foreign_toplevel_list_v1,
};

const HELP: &str = "computer-use-linux-cosmic\n\nUsage:\n  computer-use-linux-cosmic probe\n  computer-use-linux-cosmic list-windows\n  computer-use-linux-cosmic focused-window\n  computer-use-linux-cosmic activate-window --window-id <id>\n  computer-use-linux-cosmic serve";
const BACKEND: &str = "cosmic-wayland";

#[derive(Debug, Clone, Serialize, Deserialize)]
struct WindowInfo {
    window_id: u64,
    title: Option<String>,
    app_id: Option<String>,
    wm_class: Option<String>,
    pid: Option<u32>,
    bounds: Option<WindowBounds>,
    workspace: Option<i32>,
    focused: bool,
    hidden: bool,
    client_type: Option<String>,
    backend: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct WindowBounds {
    x: Option<i32>,
    y: Option<i32>,
    width: u32,
    height: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct ProbeOutput {
    ok: bool,
    can_list_windows: bool,
    can_activate_windows: bool,
    detail: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct ActivationOutput {
    ok: bool,
    detail: String,
}

#[derive(Debug, Clone, Default)]
struct ToplevelRecord {
    foreign: Option<ext_foreign_toplevel_handle_v1::ExtForeignToplevelHandleV1>,
    cosmic: Option<zcosmic_toplevel_handle_v1::ZcosmicToplevelHandleV1>,
    identifier: Option<String>,
    title: Option<String>,
    app_id: Option<String>,
    focused: bool,
    hidden: bool,
}

impl ToplevelRecord {
    fn to_window(&self) -> Option<WindowInfo> {
        self.foreign.as_ref()?;
        let identifier = self.identifier.as_deref()?;
        Some(WindowInfo {
            window_id: stable_window_id(identifier),
            title: self.title.clone().filter(|value| !value.trim().is_empty()),
            app_id: self.app_id.clone().filter(|value| !value.trim().is_empty()),
            wm_class: None,
            pid: None,
            bounds: None,
            workspace: None,
            focused: self.focused,
            hidden: self.hidden,
            client_type: Some("wayland".to_string()),
            backend: BACKEND.to_string(),
        })
    }
}

#[derive(Default)]
struct AppData {
    toplevel_info: Option<zcosmic_toplevel_info_v1::ZcosmicToplevelInfoV1>,
    toplevel_manager: Option<zcosmic_toplevel_manager_v1::ZcosmicToplevelManagerV1>,
    toplevel_list_available: bool,
    seats: Vec<wl_seat::WlSeat>,
    capabilities:
        Vec<WEnum<zcosmic_toplevel_manager_v1::ZcosmicToplelevelManagementCapabilitiesV1>>,
    records: HashMap<u32, ToplevelRecord>,
    by_cosmic_id: HashMap<u32, u32>,
}

fn main() -> Result<()> {
    match Command::parse(std::env::args().skip(1).collect())? {
        Command::Probe => print_json(&probe()?),
        Command::ListWindows => print_json(&collect_windows()?),
        Command::FocusedWindow => print_json(&focused_window()?),
        Command::ActivateWindow { window_id } => print_json(&activate_window(window_id)?),
        Command::Serve => serve(),
    }
}

#[derive(Debug)]
enum Command {
    Probe,
    ListWindows,
    FocusedWindow,
    ActivateWindow { window_id: u64 },
    Serve,
}

impl Command {
    fn parse(args: Vec<String>) -> Result<Self> {
        match args.as_slice() {
            [command] if command == "probe" => Ok(Self::Probe),
            [command] if command == "list-windows" => Ok(Self::ListWindows),
            [command] if command == "focused-window" => Ok(Self::FocusedWindow),
            [command] if command == "serve" => Ok(Self::Serve),
            [command, flag, value] if command == "activate-window" && flag == "--window-id" => {
                Ok(Self::ActivateWindow {
                    window_id: value
                        .parse::<u64>()
                        .with_context(|| format!("invalid window id {value}"))?,
                })
            }
            [command] if command == "--help" || command == "-h" => {
                println!("{HELP}");
                std::process::exit(0);
            }
            [] => {
                println!("{HELP}");
                std::process::exit(0);
            }
            _ => bail!("unknown arguments. Expected one of: probe, list-windows, focused-window, activate-window --window-id <id>, serve"),
        }
    }
}

fn probe() -> Result<ProbeOutput> {
    Ok(Snapshot::collect()?.probe())
}

fn collect_windows() -> Result<Vec<WindowInfo>> {
    Ok(Snapshot::collect()?.windows())
}

fn focused_window() -> Result<Option<WindowInfo>> {
    let snapshot = Snapshot::collect()?;
    Ok(snapshot.focused_window())
}

fn activate_window(window_id: u64) -> Result<ActivationOutput> {
    let mut snapshot = Snapshot::collect()?;
    snapshot.activate(window_id)
}

fn serve() -> Result<()> {
    let mut snapshot = Snapshot::collect()?;
    let stdin = io::stdin();
    let mut stdin = stdin.lock();
    let mut stdout = io::stdout().lock();
    while let Some(line) =
        read_cosmic_service_message(&mut stdin).context("failed to read COSMIC service request")?
    {
        let request = match serde_json::from_str::<CosmicServiceRequest>(&line) {
            Ok(request) => request,
            Err(error) => {
                write_service_response(
                    &mut stdout,
                    &CosmicServiceResponse::error(0, format!("invalid request: {error}")),
                )?;
                continue;
            }
        };
        if request.version != COSMIC_SERVICE_PROTOCOL_VERSION {
            write_service_response(
                &mut stdout,
                &CosmicServiceResponse::error(
                    request.id,
                    format!(
                        "unsupported protocol version {}; expected {COSMIC_SERVICE_PROTOCOL_VERSION}",
                        request.version
                    ),
                ),
            )?;
            continue;
        }

        snapshot.refresh()?;
        let response = match execute_service_command(&mut snapshot, request.command) {
            Ok(result) => CosmicServiceResponse::success(request.id, result),
            Err(error) => CosmicServiceResponse::error(request.id, format!("{error:#}")),
        };
        write_service_response(&mut stdout, &response)?;
    }
    Ok(())
}

fn execute_service_command(
    snapshot: &mut Snapshot,
    command: CosmicServiceCommand,
) -> Result<serde_json::Value> {
    match command {
        CosmicServiceCommand::Probe => serde_json::to_value(snapshot.probe()),
        CosmicServiceCommand::ListWindows => serde_json::to_value(snapshot.windows()),
        CosmicServiceCommand::FocusedWindow => serde_json::to_value(snapshot.focused_window()),
        CosmicServiceCommand::ActivateWindow { window_id } => {
            serde_json::to_value(snapshot.activate(window_id)?)
        }
    }
    .context("failed to serialize COSMIC service result")
}

fn write_service_response(stdout: &mut impl Write, response: &CosmicServiceResponse) -> Result<()> {
    serde_json::to_writer(&mut *stdout, response)
        .context("failed to serialize COSMIC service response")?;
    stdout
        .write_all(b"\n")
        .context("failed to terminate COSMIC service response")?;
    stdout
        .flush()
        .context("failed to flush COSMIC service response")
}

struct Snapshot {
    event_queue: wayland_client::EventQueue<AppData>,
    app_data: AppData,
}

impl Snapshot {
    fn collect() -> Result<Self> {
        let conn = Connection::connect_to_env().context("failed to connect to Wayland display")?;
        let (globals, event_queue) =
            registry_queue_init(&conn).context("failed to initialize Wayland registry queue")?;
        let mut snapshot = Self {
            event_queue,
            app_data: AppData::default(),
        };
        let qh = snapshot.event_queue.handle();
        snapshot.app_data.toplevel_info = globals
            .bind::<zcosmic_toplevel_info_v1::ZcosmicToplevelInfoV1, _, _>(&qh, 2..=3, ())
            .ok();
        snapshot.app_data.toplevel_manager = globals
            .bind::<zcosmic_toplevel_manager_v1::ZcosmicToplevelManagerV1, _, _>(&qh, 1..=4, ())
            .ok();
        globals.contents().with_list(|entries| {
            for global in entries {
                if global.interface == "wl_seat" {
                    snapshot
                        .app_data
                        .seats
                        .push(globals.registry().bind::<wl_seat::WlSeat, _, _>(
                            global.name,
                            global.version.min(9),
                            &qh,
                            (),
                        ));
                }
            }
        });
        snapshot.app_data.toplevel_list_available = globals
            .bind::<ext_foreign_toplevel_list_v1::ExtForeignToplevelListV1, _, _>(&qh, 1..=1, ())
            .is_ok();
        snapshot.prime()?;
        Ok(snapshot)
    }

    fn prime(&mut self) -> Result<()> {
        for _ in 0..4 {
            self.event_queue
                .roundtrip(&mut self.app_data)
                .context("Wayland roundtrip failed")?;
        }
        Ok(())
    }

    fn refresh(&mut self) -> Result<()> {
        self.event_queue
            .roundtrip(&mut self.app_data)
            .context("Wayland refresh roundtrip failed")?;
        Ok(())
    }

    fn windows(&self) -> Vec<WindowInfo> {
        let mut windows = self
            .app_data
            .records
            .values()
            .filter_map(ToplevelRecord::to_window)
            .collect::<Vec<_>>();
        windows.sort_by_key(|window| window.window_id);
        windows
    }

    fn focused_window(&self) -> Option<WindowInfo> {
        self.windows().into_iter().find(|window| window.focused)
    }

    fn probe(&self) -> ProbeOutput {
        let window_count = self.windows().len();
        let can_activate = self.can_activate_windows();
        ProbeOutput {
            ok: self.app_data.toplevel_list_available,
            can_list_windows: self.app_data.toplevel_list_available,
            can_activate_windows: can_activate,
            detail: if !self.app_data.toplevel_list_available {
                "COSMIC foreign toplevel listing is unavailable in this session.".to_string()
            } else if can_activate {
                format!(
                    "COSMIC foreign toplevel listing is available and activation is supported for {window_count} window(s)."
                )
            } else {
                format!(
                    "COSMIC foreign toplevel listing is available for {window_count} window(s), but activation support is incomplete."
                )
            },
        }
    }

    fn can_activate_windows(&self) -> bool {
        !self.app_data.seats.is_empty()
            && self.app_data.toplevel_manager.is_some()
            && self
                .app_data
                .records
                .values()
                .any(|record| record.cosmic.is_some())
            && self.app_data.capabilities.iter().any(|capability| {
                matches!(
                    capability,
                    WEnum::Value(
                        zcosmic_toplevel_manager_v1::ZcosmicToplelevelManagementCapabilitiesV1::Activate
                    )
                )
            })
    }

    fn activate(&mut self, window_id: u64) -> Result<ActivationOutput> {
        if !self.can_activate_windows() {
            return Ok(ActivationOutput {
                ok: false,
                detail: "COSMIC activation capability is unavailable.".to_string(),
            });
        }
        let seat = self
            .app_data
            .seats
            .first()
            .cloned()
            .ok_or_else(|| anyhow!("activation capability advertised without a wl_seat"))?;
        let cosmic = self
            .app_data
            .records
            .values()
            .find(|record| {
                record
                    .identifier
                    .as_deref()
                    .is_some_and(|id| stable_window_id(id) == window_id)
            })
            .and_then(|record| record.cosmic.clone());
        let Some(cosmic) = cosmic else {
            return Ok(ActivationOutput {
                ok: false,
                detail: format!("No activatable COSMIC toplevel matched window_id {window_id}."),
            });
        };
        let manager = self
            .app_data
            .toplevel_manager
            .as_ref()
            .ok_or_else(|| anyhow!("COSMIC toplevel management protocol not advertised"))?;
        manager.activate(&cosmic, &seat);
        self.event_queue
            .roundtrip(&mut self.app_data)
            .context("Wayland roundtrip after activation failed")?;
        let focused = self.app_data.records.values().any(|record| {
            record.focused
                && record
                    .identifier
                    .as_deref()
                    .is_some_and(|identifier| stable_window_id(identifier) == window_id)
        });
        Ok(ActivationOutput {
            ok: focused,
            detail: if focused {
                format!("COSMIC confirmed activation for window_id {window_id}.")
            } else {
                format!(
                    "COSMIC did not confirm activation for window_id {window_id}; the compositor may have denied the request."
                )
            },
        })
    }
}

impl Dispatch<wl_registry::WlRegistry, GlobalListContents> for AppData {
    fn event(
        app_data: &mut Self,
        registry: &wl_registry::WlRegistry,
        event: wl_registry::Event,
        _: &GlobalListContents,
        _: &Connection,
        qh: &QueueHandle<Self>,
    ) {
        if let wl_registry::Event::Global {
            name,
            interface,
            version,
        } = event
        {
            match interface.as_str() {
                "ext_foreign_toplevel_list_v1" => {
                    registry.bind::<ext_foreign_toplevel_list_v1::ExtForeignToplevelListV1, _, _>(
                        name,
                        1,
                        qh,
                        (),
                    );
                    app_data.toplevel_list_available = true;
                }
                "zcosmic_toplevel_info_v1" if version >= 2 => {
                    let info = registry
                        .bind::<zcosmic_toplevel_info_v1::ZcosmicToplevelInfoV1, _, _>(
                            name,
                            version.min(3),
                            qh,
                            (),
                        );
                    for (foreign_id, record) in &mut app_data.records {
                        if record.cosmic.is_some() {
                            continue;
                        }
                        let Some(foreign) = record.foreign.as_ref() else {
                            continue;
                        };
                        let cosmic = info.get_cosmic_toplevel(foreign, qh, ());
                        app_data
                            .by_cosmic_id
                            .insert(cosmic.id().protocol_id(), *foreign_id);
                        record.cosmic = Some(cosmic);
                    }
                    app_data.toplevel_info = Some(info);
                }
                "zcosmic_toplevel_manager_v1" => {
                    app_data.toplevel_manager = Some(
                        registry
                            .bind::<zcosmic_toplevel_manager_v1::ZcosmicToplevelManagerV1, _, _>(
                                name,
                                version.min(4),
                                qh,
                                (),
                            ),
                    );
                }
                "wl_seat" => {
                    app_data.seats.push(registry.bind::<wl_seat::WlSeat, _, _>(
                        name,
                        version.min(9),
                        qh,
                        (),
                    ));
                }
                _ => {}
            }
        }
    }
}

impl Dispatch<ext_foreign_toplevel_list_v1::ExtForeignToplevelListV1, ()> for AppData {
    fn event(
        app_data: &mut Self,
        list: &ext_foreign_toplevel_list_v1::ExtForeignToplevelListV1,
        event: ext_foreign_toplevel_list_v1::Event,
        _: &(),
        _conn: &Connection,
        qh: &QueueHandle<Self>,
    ) {
        match event {
            ext_foreign_toplevel_list_v1::Event::Toplevel { toplevel } => {
                let foreign_id = toplevel.id().protocol_id();
                let mut record = ToplevelRecord {
                    foreign: Some(toplevel.clone()),
                    ..Default::default()
                };
                if let Some(info) = app_data.toplevel_info.as_ref() {
                    let cosmic = info.get_cosmic_toplevel(&toplevel, qh, ());
                    app_data
                        .by_cosmic_id
                        .insert(cosmic.id().protocol_id(), foreign_id);
                    record.cosmic = Some(cosmic);
                }
                app_data.records.insert(foreign_id, record);
            }
            ext_foreign_toplevel_list_v1::Event::Finished => {
                app_data.toplevel_list_available = false;
                list.destroy();
            }
            _ => unreachable!(),
        }
    }

    event_created_child!(
        AppData,
        ext_foreign_toplevel_list_v1::ExtForeignToplevelListV1,
        [
            ext_foreign_toplevel_list_v1::EVT_TOPLEVEL_OPCODE => (ext_foreign_toplevel_handle_v1::ExtForeignToplevelHandleV1, ()),
        ]
    );
}

impl Dispatch<ext_foreign_toplevel_handle_v1::ExtForeignToplevelHandleV1, ()> for AppData {
    fn event(
        app_data: &mut Self,
        handle: &ext_foreign_toplevel_handle_v1::ExtForeignToplevelHandleV1,
        event: ext_foreign_toplevel_handle_v1::Event,
        _: &(),
        _conn: &Connection,
        _qh: &QueueHandle<Self>,
    ) {
        let foreign_id = handle.id().protocol_id();
        let Some(record) = app_data.records.get_mut(&foreign_id) else {
            return;
        };
        match event {
            ext_foreign_toplevel_handle_v1::Event::Identifier { identifier } => {
                record.identifier = Some(identifier);
            }
            ext_foreign_toplevel_handle_v1::Event::Title { title } => {
                record.title = Some(title);
            }
            ext_foreign_toplevel_handle_v1::Event::AppId { app_id } => {
                record.app_id = Some(app_id);
            }
            ext_foreign_toplevel_handle_v1::Event::Done => {}
            ext_foreign_toplevel_handle_v1::Event::Closed => {
                if let Some(record) = app_data.records.remove(&foreign_id) {
                    if let Some(cosmic) = record.cosmic {
                        app_data.by_cosmic_id.remove(&cosmic.id().protocol_id());
                        cosmic.destroy();
                    }
                    handle.destroy();
                }
            }
            _ => unreachable!(),
        }
    }
}

impl Dispatch<zcosmic_toplevel_info_v1::ZcosmicToplevelInfoV1, ()> for AppData {
    fn event(
        _app_data: &mut Self,
        _info: &zcosmic_toplevel_info_v1::ZcosmicToplevelInfoV1,
        _event: zcosmic_toplevel_info_v1::Event,
        _: &(),
        _: &Connection,
        _: &QueueHandle<Self>,
    ) {
    }

    event_created_child!(
        AppData,
        zcosmic_toplevel_info_v1::ZcosmicToplevelInfoV1,
        [
            zcosmic_toplevel_info_v1::EVT_TOPLEVEL_OPCODE => (zcosmic_toplevel_handle_v1::ZcosmicToplevelHandleV1, ()),
        ]
    );
}

impl Dispatch<zcosmic_toplevel_handle_v1::ZcosmicToplevelHandleV1, ()> for AppData {
    fn event(
        app_data: &mut Self,
        handle: &zcosmic_toplevel_handle_v1::ZcosmicToplevelHandleV1,
        event: zcosmic_toplevel_handle_v1::Event,
        _: &(),
        _: &Connection,
        _: &QueueHandle<Self>,
    ) {
        let cosmic_id = handle.id().protocol_id();
        let Some(foreign_id) = app_data.by_cosmic_id.get(&cosmic_id).copied() else {
            return;
        };
        if matches!(&event, zcosmic_toplevel_handle_v1::Event::Closed) {
            app_data.by_cosmic_id.remove(&cosmic_id);
            if let Some(record) = app_data.records.get_mut(&foreign_id) {
                record.cosmic = None;
                record.focused = false;
                record.hidden = false;
            }
            handle.destroy();
            return;
        }
        let Some(record) = app_data.records.get_mut(&foreign_id) else {
            return;
        };
        match event {
            zcosmic_toplevel_handle_v1::Event::State { state } => {
                record.focused = false;
                record.hidden = false;
                for value in state.chunks_exact(4) {
                    if let Ok(parsed) =
                        zcosmic_toplevel_handle_v1::State::try_from(u32::from_ne_bytes([
                            value[0], value[1], value[2], value[3],
                        ]))
                    {
                        if parsed == zcosmic_toplevel_handle_v1::State::Activated {
                            record.focused = true;
                        }
                        if parsed == zcosmic_toplevel_handle_v1::State::Minimized {
                            record.hidden = true;
                        }
                    }
                }
            }
            zcosmic_toplevel_handle_v1::Event::Geometry { .. }
            | zcosmic_toplevel_handle_v1::Event::OutputEnter { .. }
            | zcosmic_toplevel_handle_v1::Event::OutputLeave { .. }
            | zcosmic_toplevel_handle_v1::Event::WorkspaceEnter { .. }
            | zcosmic_toplevel_handle_v1::Event::WorkspaceLeave { .. }
            | zcosmic_toplevel_handle_v1::Event::ExtWorkspaceEnter { .. }
            | zcosmic_toplevel_handle_v1::Event::ExtWorkspaceLeave { .. }
            | zcosmic_toplevel_handle_v1::Event::Title { .. }
            | zcosmic_toplevel_handle_v1::Event::AppId { .. }
            | zcosmic_toplevel_handle_v1::Event::Done => {}
            _ => unreachable!(),
        }
    }
}

impl Dispatch<zcosmic_toplevel_manager_v1::ZcosmicToplevelManagerV1, ()> for AppData {
    fn event(
        app_data: &mut Self,
        _manager: &zcosmic_toplevel_manager_v1::ZcosmicToplevelManagerV1,
        event: zcosmic_toplevel_manager_v1::Event,
        _: &(),
        _: &Connection,
        _: &QueueHandle<Self>,
    ) {
        match event {
            zcosmic_toplevel_manager_v1::Event::Capabilities { capabilities } => {
                app_data.capabilities = capabilities
                    .chunks_exact(4)
                    .map(|chunk| {
                        WEnum::from(u32::from_ne_bytes([chunk[0], chunk[1], chunk[2], chunk[3]]))
                    })
                    .collect();
            }
            _ => unreachable!(),
        }
    }
}

impl Dispatch<wl_seat::WlSeat, ()> for AppData {
    fn event(
        _app_data: &mut Self,
        _seat: &wl_seat::WlSeat,
        _event: wl_seat::Event,
        _: &(),
        _: &Connection,
        _: &QueueHandle<Self>,
    ) {
    }
}

fn stable_window_id(identifier: &str) -> u64 {
    fnv1a_64(identifier.as_bytes())
}

fn fnv1a_64(bytes: &[u8]) -> u64 {
    let mut hash = 0xcbf29ce484222325u64;
    for byte in bytes {
        hash ^= u64::from(*byte);
        hash = hash.wrapping_mul(0x100000001b3);
    }
    hash
}

fn print_json<T: Serialize>(value: &T) -> Result<()> {
    println!(
        "{}",
        serde_json::to_string_pretty(value).context("failed to serialize JSON output")?
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_activate_window_args_requires_numeric_id() {
        let error = Command::parse(vec![
            "activate-window".to_string(),
            "--window-id".to_string(),
            "nope".to_string(),
        ])
        .unwrap_err()
        .to_string();

        assert!(error.contains("invalid window id"));
    }

    #[test]
    fn stable_window_id_is_stable() {
        assert_eq!(stable_window_id("window-1"), stable_window_id("window-1"));
    }

    #[test]
    fn parse_serve_command() {
        assert!(matches!(
            Command::parse(vec!["serve".to_string()]).unwrap(),
            Command::Serve
        ));
    }
}
