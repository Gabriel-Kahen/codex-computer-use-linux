use crate::accessibility_snapshot::{
    AccessibilitySnapshot, AccessibilitySnapshotStore, AccessibilitySnapshotTarget,
};
use crate::action_batch::{
    bounded_action_result_message, execute_action_batch, ActionBatchOutput, ActionBatchParams,
    ActionOutput, BatchAction, BatchActionRun, BatchClick, NON_EDITABLE_TEXT_LANDING_WARNING,
    NO_FOCUSED_ELEMENT_TEXT_LANDING_WARNING,
};
use crate::atspi_tree::{
    focused_element_summary, list_accessible_apps, perform_action_by_identity, set_element_value,
    snapshot_compact_tree, AccessibilityAction, AccessibilityNode, AccessibleAppSummary,
    ActionFingerprint, Bounds, FocusedElementSummary, ValueSetInvocation,
};
use crate::claim_coordination::{acquire_mutation_guards, ClaimContext, Coordinator, MutationLane};
use crate::desktop_transaction::DesktopTransaction;
use crate::diagnostics::{doctor_report, setup_accessibility_report, DoctorReport, SetupReport};
use crate::gnome_extension::{setup_window_targeting_report, WindowTargetingSetupReport};
use crate::input_policy::PointerInputOverrides;
use crate::observation::{
    prepare_visual_captures, AdaptiveObservationMetadata, ObservationMode, ObservationRegion,
    ObservationTracker, VisualObservationKind, VisualPlan, DEFAULT_CHECKPOINT_INTERVAL,
};
use crate::pointer_dispatch::{
    observed_element_pointer_target, pointer_dispatch_verification, run_verified_pointer_dispatch,
    verify_pointer_dispatch, ObservedElementPointer, PointerDispatchBoundary,
    PointerDispatchVerification,
};
use crate::remote_desktop::{
    keysyms_for_text, press_keycode_chord, scroll as portal_scroll, start_portal_session,
    type_text_with_keysyms, PortalActionError, PortalKeyboardSession, PortalPointerSession,
    PortalSession, ScrollDirection,
};
use crate::screenshot::{
    capture_screenshot_raw, capture_screenshot_raw_recent, prepare_screenshot_payload,
    RawScreenshotCapture, ScreenshotCapture, ScreenshotOutputFormat, ScreenshotPayloadOptions,
};
use crate::scroll_target::{resolve_observed_scroll_target, ScrollTargetRequest};
use crate::windowing::capture_window_exact;
use crate::windowing::registry;
use crate::windows::{
    focus_window_target, focused_window, list_windows, resolve_window_target,
    window_permission_hint, WindowFocusResult, WindowInfo, WindowTarget,
    GNOME_SHELL_INTROSPECT_BACKEND,
};
use crate::ydotool;
use anyhow::Result;
use rmcp::{
    handler::server::wrapper::{Json, Parameters},
    model::{CallToolResult, Content},
    schemars::JsonSchema,
    tool, tool_handler, tool_router, ErrorData, ServerHandler, ServiceExt,
};
use serde::{Deserialize, Serialize};
use std::{
    env,
    future::Future,
    os::unix::net::{UnixDatagram, UnixStream},
    path::PathBuf,
    process::{Command, Output, Stdio},
    sync::{Arc, Mutex, OnceLock},
    time::{Duration, Instant},
};
use tokio::{
    io::{AsyncRead, AsyncReadExt, AsyncWriteExt},
    process::{Child as TokioChild, Command as TokioCommand},
    time::{sleep, timeout},
};
use zbus::{Connection as ZbusConnection, Proxy as ZbusProxy};

#[path = "click_target.rs"]
mod click_target;

const YDOTOOL_TIMEOUT: Duration = Duration::from_secs(10);
const YDOTOOL_TYPE_CHARS_PER_SECOND: u64 = 20;
const KDE_CLIPBOARD_DBUS_TIMEOUT: Duration = Duration::from_secs(3);
const KDE_KLIPPER_SERVICE: &str = "org.kde.klipper";
const KDE_KLIPPER_PATH: &str = "/klipper";
const KDE_KLIPPER_INTERFACE: &str = "org.kde.klipper.klipper";
const READINESS_CACHE_TTL: Duration = Duration::from_secs(2);

#[derive(Clone, Copy)]
enum ClaimGuardMode {
    Acquire,
    AlreadyHeld,
}

#[derive(Default)]
struct DiagnosticsCache {
    generation: u64,
    report: Option<(Instant, DoctorReport)>,
}

#[derive(Clone, Default)]
pub struct ComputerUseLinux {
    accessibility_snapshots: Arc<Mutex<AccessibilitySnapshotStore>>,
    observation_tracker: Arc<Mutex<ObservationTracker>>,
    adaptive_observation_lock: Arc<tokio::sync::Mutex<()>>,
    portal_session: Arc<Mutex<Option<PortalSession>>>,
    /// Lazily-created uinput absolute pointer (preferred coordinate backend).
    abs_pointer: Arc<Mutex<Option<crate::abs_pointer::AbsPointer>>>,
    portal_session_init_lock: Arc<tokio::sync::Mutex<()>>,
    kde_clipboard_lock: Arc<tokio::sync::Mutex<()>>,
    desktop_transaction: DesktopTransaction,
    /// Cached logical desktop size (union of monitors) from the most recent
    /// full-frame capture; used for off-screen window/coordinate warnings.
    desktop_size: Arc<Mutex<Option<(u32, u32)>>>,
    diagnostics_cache: Arc<Mutex<DiagnosticsCache>>,
    claim_coordinator: Arc<OnceLock<Option<Coordinator>>>,
}

fn sanitize_unsigned_integer_formats(value: &mut serde_json::Value) {
    let serde_json::Value::Object(object) = value else {
        return;
    };

    let has_unsigned_format = matches!(
        object.get("format").and_then(serde_json::Value::as_str),
        Some("uint" | "uint8" | "uint16" | "uint32" | "uint64" | "usize")
    );
    if has_unsigned_format {
        object.remove("format");
    }

    for nested in object.values_mut() {
        match nested {
            serde_json::Value::Object(_) => sanitize_unsigned_integer_formats(nested),
            serde_json::Value::Array(items) => {
                for item in items {
                    sanitize_unsigned_integer_formats(item);
                }
            }
            _ => {}
        }
    }
}

impl ComputerUseLinux {
    fn diagnostics(&self) -> DoctorReport {
        let mut cache = self
            .diagnostics_cache
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if let Some((_, report)) = cache
            .report
            .as_ref()
            .filter(|(captured_at, _)| captured_at.elapsed() <= READINESS_CACHE_TTL)
        {
            return report.clone();
        }
        let report = doctor_report();
        cache.report = Some((Instant::now(), report.clone()));
        report
    }

    fn invalidate_diagnostics(&self) {
        let mut cache = self
            .diagnostics_cache
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        cache.generation = cache.generation.wrapping_add(1);
        cache.report = None;
    }

    fn mcp_tool_router(&self) -> rmcp::handler::server::router::tool::ToolRouter<Self> {
        let mut router = Self::tool_router();
        for route in router.map.values_mut() {
            let input_schema = Arc::make_mut(&mut route.attr.input_schema);
            for value in input_schema.values_mut() {
                sanitize_unsigned_integer_formats(value);
            }
            if let Some(output_schema) = route.attr.output_schema.as_mut() {
                for value in Arc::make_mut(output_schema).values_mut() {
                    sanitize_unsigned_integer_formats(value);
                }
            }
        }
        router
    }
}

#[tool_router]
impl ComputerUseLinux {
    #[tool(
        name = "doctor",
        description = "Report Linux Computer Use desktop integration readiness.",
        annotations(
            read_only_hint = true,
            destructive_hint = false,
            idempotent_hint = true,
            open_world_hint = false
        )
    )]
    fn doctor(&self) -> Json<DoctorReport> {
        Json(doctor_report())
    }

    #[tool(
        name = "setup_accessibility",
        description = "Enable GNOME accessibility through gsettings so Linux Computer Use can read AT-SPI trees.",
        annotations(
            read_only_hint = false,
            destructive_hint = false,
            idempotent_hint = true,
            open_world_hint = false
        )
    )]
    fn setup_accessibility(&self) -> Json<SetupReport> {
        let report = setup_accessibility_report();
        self.invalidate_diagnostics();
        Json(report)
    }

    #[tool(
        name = "setup_window_targeting",
        description = "Install and enable the optional GNOME Shell extension used for exact window list/focus targeting when GNOME blocks native introspection.",
        annotations(
            read_only_hint = false,
            destructive_hint = false,
            idempotent_hint = true,
            open_world_hint = false
        )
    )]
    async fn setup_window_targeting(&self) -> Json<WindowTargetingSetupReport> {
        let report = setup_window_targeting_report().await;
        self.invalidate_diagnostics();
        Json(report)
    }

    #[tool(
        name = "list_apps",
        description = "List running Linux desktop app candidates visible to the Computer Use backend.",
        annotations(
            read_only_hint = true,
            destructive_hint = false,
            idempotent_hint = true,
            open_world_hint = true
        )
    )]
    async fn list_apps(&self) -> Json<ListAppsOutput> {
        let (accessible_apps, accessibility_error) = match list_accessible_apps(50).await {
            Ok(apps) => (apps, None),
            Err(error) => (Vec::new(), Some(format!("{error:#}"))),
        };

        Json(ListAppsOutput {
            apps: list_process_apps(),
            accessible_apps,
            accessibility_error,
            note: "Linux Computer Use lists process candidates plus AT-SPI application roots when accessibility is enabled.".to_string(),
        })
    }

    #[tool(
        name = "list_windows",
        description = "List compositor windows with title, app id, class, focus state, client type, and known bounds.",
        annotations(
            read_only_hint = true,
            destructive_hint = false,
            idempotent_hint = true,
            open_world_hint = true
        )
    )]
    async fn list_windows(&self) -> Json<ListWindowsOutput> {
        Json(window_list_output().await)
    }

    #[tool(
        name = "focused_window",
        description = "Return the compositor window that currently has keyboard focus.",
        annotations(
            read_only_hint = true,
            destructive_hint = false,
            idempotent_hint = true,
            open_world_hint = true
        )
    )]
    async fn focused_window(&self) -> Json<FocusedWindowOutput> {
        match focused_window().await {
            Ok(window) => {
                let backend = window_backend(window.as_ref().into_iter());
                Json(FocusedWindowOutput {
                    backend,
                    focused_window: window,
                    error: None,
                    permissions_hint: None,
                    message:
                        "Focused window query completed through the available compositor window backend."
                            .to_string(),
                })
            }
            Err(error) => {
                let error = format!("{error:#}");
                Json(FocusedWindowOutput {
                    backend: GNOME_SHELL_INTROSPECT_BACKEND.to_string(),
                    focused_window: None,
                    permissions_hint: window_permission_hint(&error),
                    error: Some(error),
                    message: "Focused window query failed; targeted keyboard input is unavailable until window introspection works.".to_string(),
                })
            }
        }
    }

    #[tool(
        name = "activate_window",
        description = "Focus a Linux desktop window by window_id, pid, app_id, wm_class, title, or terminal selectors when the compositor permits it.",
        annotations(
            read_only_hint = false,
            destructive_hint = false,
            idempotent_hint = true,
            open_world_hint = true
        )
    )]
    async fn activate_window(
        &self,
        Parameters(params): Parameters<ActivateWindowParams>,
    ) -> Json<ActivateWindowOutput> {
        let owner = self.clone();
        self.desktop_transaction
            .run(move || async move { owner.activate_window_unlocked(params).await })
            .await
    }

    async fn activate_window_unlocked(
        &self,
        params: ActivateWindowParams,
    ) -> Json<ActivateWindowOutput> {
        let claim = params.claim.clone();
        let mut target = params.into_target();
        let received = Some(serde_json::json!(target.clone()));
        let activation_error = |error: String| {
            Json(ActivateWindowOutput {
                ok: false,
                implemented: true,
                backend: GNOME_SHELL_INTROSPECT_BACKEND.to_string(),
                focus: None,
                permissions_hint: window_permission_hint(&error),
                error: Some(error),
                received: received.clone(),
            })
        };
        let coordination_window_id = match self.coordination_window_id(Some(&target)).await {
            Ok(window_id) => window_id,
            Err(error) => return activation_error(error),
        };
        if let Some(window_id) = coordination_window_id {
            target.pin_exact_window_id(window_id);
        }
        let _claim_guard = match self
            .mutation_claim_guard(
                ClaimGuardMode::Acquire,
                coordination_window_id,
                &claim,
                MutationLane::PhysicalSeat,
            )
            .await
        {
            Ok(guard) => guard,
            Err(error) => return activation_error(error),
        };
        match focus_window_target(&target).await {
            Ok(focus) => {
                let ok = focus_satisfies_target(&focus, &target);
                Json(ActivateWindowOutput {
                    ok,
                    implemented: true,
                    backend: focus.backend.clone(),
                    focus: Some(focus),
                    error: None,
                    permissions_hint: None,
                    received: received.clone(),
                })
            }
            Err(error) => {
                let error = format!("{error:#}");
                activation_error(error)
            }
        }
    }

    #[tool(
        name = "get_app_state",
        description = "Start an app use session if needed, then get bounded screenshot and accessibility state. Successful accessibility snapshots include an opaque observation_id that must be echoed for element-targeted clicks, element-targeted scrolls, and direct semantic actions. Legacy calls return a full visual observation. observation_mode=adaptive returns a full screenshot checkpoint unless base_checkpoint_id matches the caller's last adaptive result; matching calls return unchanged summaries or changed regions relative to that checkpoint. Targeted screenshots use window-local coordinates; add coordinate_origin_x/y when an off-screen window was clipped. AT-SPI bounds remain in desktop coordinates.",
        output_schema = rmcp::handler::server::tool::schema_for_type::<GetAppStateOutput>(),
        annotations(
            read_only_hint = true,
            destructive_hint = false,
            idempotent_hint = true,
            open_world_hint = true
        )
    )]
    async fn get_app_state(
        &self,
        Parameters(params): Parameters<GetAppStateParams>,
    ) -> Result<CallToolResult, ErrorData> {
        let owner = self.clone();
        self.desktop_transaction
            .run(move || async move {
                owner
                    .get_app_state_unlocked(params, ClaimGuardMode::Acquire)
                    .await
            })
            .await
    }

    async fn get_app_state_unlocked(
        &self,
        params: GetAppStateParams,
        claim_guard_mode: ClaimGuardMode,
    ) -> Result<CallToolResult, ErrorData> {
        let observation_mode = params.observation_mode;
        let adaptive = observation_mode == Some(ObservationMode::Adaptive);
        let _adaptive_guard = if adaptive {
            Some(self.adaptive_observation_lock.lock().await)
        } else {
            None
        };
        let verbose = params.verbose.unwrap_or(false);
        let diagnostics_owner = self.clone();
        let (diagnostics, (window_context, window_error, window_permissions_hint)) = tokio::join!(
            tokio::task::spawn_blocking(move || diagnostics_owner.diagnostics()),
            self.resolve_window_context(&params),
        );
        let diagnostics = diagnostics.map_err(|error| {
            ErrorData::internal_error(format!("diagnostics task failed: {error}"), None)
        })?;
        let max_nodes = params.max_nodes.unwrap_or(120).clamp(1, 500);
        let max_depth = params.max_depth.unwrap_or(12).min(12);
        let include_screenshot = params.include_screenshot.unwrap_or(true);
        let _claim_guard = if include_screenshot {
            self.mutation_claim_guard(
                claim_guard_mode,
                window_context.as_ref().map(|window| window.window_id),
                &params.claim,
                MutationLane::Window,
            )
            .await
            .map_err(|error| ErrorData::invalid_params(error, None))?
        } else {
            None
        };
        let screenshot_options = params.screenshot_options();
        let screenshot_target_requested = params.window_target().has_target();
        let screenshot_future = async {
            if !include_screenshot {
                return (None, None);
            }

            if screenshot_target_requested && window_context.is_none() {
                let error = window_error
                    .as_deref()
                    .unwrap_or("the requested target window could not be resolved");
                return (
                    None,
                    Some(format!(
                        "targeted screenshot window resolution failed: {error}; refusing to capture the full desktop"
                    )),
                );
            }

            let exact_capture = match window_context.as_ref() {
                Some(window) => match capture_window_exact(window).await {
                    Ok(capture) => capture,
                    Err(error) => {
                        return (
                            None,
                            Some(format!(
                                "targeted exact window capture failed: {error:#}; refusing to capture the full desktop"
                            )),
                        );
                    }
                },
                None => None,
            };
            let use_exact_capture = exact_capture.is_some();
            let bounds = if screenshot_target_requested && !use_exact_capture {
                match validated_target_bounds(window_context.as_ref()) {
                    Ok(bounds) => Some(bounds),
                    Err(error) => return (None, Some(format!("{error:#}"))),
                }
            } else {
                None
            };
            let raw_capture = match exact_capture {
                Some(capture) => capture,
                None => match capture_screenshot_raw_recent().await {
                    Ok(capture) => capture,
                    Err(error) => return (None, Some(format!("{error:#}"))),
                },
            };
            let prepared = tokio::task::spawn_blocking(move || {
                crop_raw_screenshot(
                    raw_capture,
                    bounds.as_ref(),
                    screenshot_target_requested && !use_exact_capture,
                )
            })
            .await;
            match prepared {
                Ok(Ok((capture, origin))) => (Some((capture, origin.unwrap_or((0, 0)))), None),
                Ok(Err(error)) => (None, Some(format!("{error:#}"))),
                Err(error) => (
                    None,
                    Some(format!("screenshot preparation task failed: {error}")),
                ),
            }
        };
        let accessibility_future = async {
            if diagnostics.readiness.can_build_accessibility_tree {
                let app_filter = self
                    .resolve_accessibility_app_filter(&params, window_context.as_ref())
                    .await;
                let target_pid = window_context.as_ref().and_then(|window| window.pid);
                match snapshot_compact_tree(app_filter.as_deref(), target_pid, max_nodes, max_depth)
                    .await
                {
                    Ok(nodes) => {
                        let raw_count = nodes.len();
                        (compact_accessibility_tree(nodes), raw_count, None)
                    }
                    Err(error) => (Vec::new(), 0, Some(format!("{error:#}"))),
                }
            } else {
                (
                    Vec::new(),
                    0,
                    Some(
                        "GNOME accessibility is disabled; call setup_accessibility first."
                            .to_string(),
                    ),
                )
            }
        };
        let (
            (raw_screenshot_with_origin, mut screenshot_error),
            (accessibility_tree, accessibility_tree_raw_count, accessibility_error),
        ) = tokio::join!(screenshot_future, accessibility_future);
        let (raw_screenshot, screenshot_origin) = raw_screenshot_with_origin
            .map_or((None, (0, 0)), |(capture, origin)| (Some(capture), origin));
        if window_error.is_some() || screenshot_error.is_some() || accessibility_error.is_some() {
            self.invalidate_diagnostics();
        }
        let accessibility_snapshot_target =
            accessibility_snapshot_target(&params, window_context.as_ref());
        let observation_id = accessibility_snapshot_target.as_ref().and_then(|target| {
            if accessibility_error.is_none() {
                Some(self.record_accessibility_snapshot(target.clone(), &accessibility_tree))
            } else {
                self.invalidate_accessibility_snapshot(target);
                None
            }
        });

        let (observation, visual_plan, raw_screenshot) = if adaptive {
            let target_key = window_context.as_ref().map_or_else(
                || {
                    params
                        .app_name_or_bundle_identifier
                        .as_deref()
                        .unwrap_or("desktop")
                        .to_string()
                },
                |window| format!("window:{}", window.window_id),
            );
            let key = format!(
                "{target_key}|{:?}",
                (
                    screenshot_options.max_width,
                    screenshot_options.max_height,
                    screenshot_options.max_bytes,
                    screenshot_options.scale,
                    screenshot_options.format,
                    screenshot_options.quality,
                    screenshot_origin,
                )
            );
            let tracker = {
                let mut tracker = self.observation_tracker.lock().map_err(|_| {
                    ErrorData::internal_error("observation tracker lock failed", None)
                })?;
                std::mem::take(&mut *tracker)
            };
            let base_checkpoint_id = params.base_checkpoint_id.clone();
            let checkpoint_interval = params
                .checkpoint_interval
                .unwrap_or(DEFAULT_CHECKPOINT_INTERVAL)
                .clamp(1, 32);
            let force_checkpoint = params.force_checkpoint.unwrap_or(false);
            let (tracker, raw_screenshot, plan) = tokio::task::spawn_blocking(move || {
                let mut tracker = tracker;
                let plan = tracker.observe(
                    key,
                    raw_screenshot.as_ref(),
                    base_checkpoint_id.as_deref(),
                    checkpoint_interval,
                    force_checkpoint,
                );
                (tracker, raw_screenshot, plan)
            })
            .await
            .map_err(|error| {
                ErrorData::internal_error(
                    format!("adaptive observation planning task failed: {error}"),
                    None,
                )
            })?;
            *self.observation_tracker.lock().map_err(|_| {
                ErrorData::internal_error("observation tracker lock failed", None)
            })? = tracker;
            let plan = plan.map_err(|error| {
                ErrorData::internal_error(
                    format!("adaptive observation planning failed: {error}"),
                    None,
                )
            })?;
            (Some(plan.metadata), plan.visual, raw_screenshot)
        } else {
            (
                None,
                if raw_screenshot.is_some() {
                    VisualPlan::Full
                } else {
                    VisualPlan::None
                },
                raw_screenshot,
            )
        };
        let (captures, preparation_error) = if let Some(raw) = raw_screenshot {
            match tokio::task::spawn_blocking(move || {
                if adaptive {
                    prepare_visual_captures(&raw, &visual_plan, screenshot_options)
                } else {
                    let width = raw.width;
                    let height = raw.height;
                    prepare_screenshot_payload(raw, screenshot_options).map(|capture| {
                        vec![(
                            ObservationRegion {
                                x: 0,
                                y: 0,
                                width,
                                height,
                            },
                            capture,
                        )]
                    })
                }
            })
            .await
            {
                Ok(Ok(captures)) => (captures, None),
                Ok(Err(error)) => (Vec::new(), Some(format!("{error:#}"))),
                Err(error) => (
                    Vec::new(),
                    Some(format!("screenshot preparation task failed: {error}")),
                ),
            }
        } else {
            (Vec::new(), None)
        };
        if let Some(error) = preparation_error {
            screenshot_error = Some(error);
            if adaptive {
                let _ = self.observation_tracker.lock().map(|mut tracker| {
                    *tracker = ObservationTracker::default();
                });
            }
        };
        let screenshot = captures.first().and_then(|(_, capture)| {
            (!adaptive
                || observation
                    .as_ref()
                    .is_some_and(|metadata| metadata.visual_kind == VisualObservationKind::Full))
            .then_some(capture)
        });
        let screenshot_regions = if adaptive {
            captures
                .iter()
                .map(|(region, capture)| {
                    ScreenshotRegionMetadata::from_capture(
                        *region,
                        capture,
                        window_context.as_ref(),
                        screenshot_origin,
                    )
                })
                .collect()
        } else {
            Vec::new()
        };
        let mut message = if let Some(metadata) = &observation {
            format!(
                "Adaptive observation {} returned {:?} pixels and {} accessibility nodes (compacted from {}).",
                metadata.sequence,
                metadata.visual_kind,
                accessibility_tree.len(),
                accessibility_tree_raw_count,
            )
        } else if let Some(error) = &accessibility_error {
            format!("MCP registration is working, but AT-SPI tree extraction failed: {error}")
        } else if let Some(capture) = screenshot {
            format!(
                "MCP registration, screenshot capture, and AT-SPI tree extraction are working. Captured {} accessibility nodes (compacted from {}) and a screenshot through {}.",
                accessibility_tree.len(),
                accessibility_tree_raw_count,
                capture.source
            )
        } else if let Some(error) = &screenshot_error {
            format!(
                "MCP registration and AT-SPI tree extraction are working. Captured {} accessibility nodes (compacted from {}). Screenshot capture failed: {error}",
                accessibility_tree.len(),
                accessibility_tree_raw_count,
            )
        } else {
            format!(
                "MCP registration and AT-SPI tree extraction are working. Captured {} accessibility nodes (compacted from {}). Screenshot capture was not requested.",
                accessibility_tree.len(),
                accessibility_tree_raw_count,
            )
        };
        if let Some(window) = &window_context {
            message.push_str(&format!(
                " Window target resolved to window_id {}.",
                window.window_id
            ));
        } else if let Some(error) = &window_error {
            message.push_str(&format!(" Window target resolution failed: {error}"));
        }

        // Full diagnostics are huge (portal/process dumps); emit them only on
        // request. The compact readiness block always travels, and failures get
        // a pointer to verbose=true instead of an automatic dump.
        let readiness = diagnostics.readiness.clone();
        let include_full = verbose;
        if !include_full
            && (accessibility_error.is_some()
                || screenshot_error.is_some()
                || window_error.is_some())
        {
            message.push_str(" Pass verbose=true for full diagnostics.");
        }
        let screenshot_metadata = screenshot.map(|capture| {
            ScreenshotMetadata::from_capture(capture, window_context.as_ref(), screenshot_origin)
        });
        let output = GetAppStateOutput {
            app_name_or_bundle_identifier: params.app_name_or_bundle_identifier,
            window_context,
            window_error,
            window_permissions_hint,
            backend: "linux-atspi".to_string(),
            screenshot: screenshot_metadata,
            screenshot_regions,
            screenshot_error,
            accessibility_tree,
            accessibility_tree_raw_count,
            observation_id,
            accessibility_coordinate_space: "desktop".to_string(),
            accessibility_error,
            readiness,
            diagnostics: include_full.then_some(diagnostics),
            observation,
            message,
        };
        app_state_tool_result(
            output,
            &captures
                .iter()
                .map(|(_, capture)| capture)
                .collect::<Vec<_>>(),
        )
    }

    #[tool(
        name = "screenshot",
        description = "Capture the screen and return it as a viewable, size-bounded image. Targeted Hyprland captures use exact compositor pixels; pass raise_window=false to avoid focusing. Other backends crop the resolved window before resize. Targeted failures never return desktop pixels. Window-targeted coordinates are window-local: add coordinate_origin_x/y before using relative=true with the same target.",
        annotations(
            read_only_hint = false,
            destructive_hint = false,
            idempotent_hint = false,
            open_world_hint = true
        )
    )]
    async fn screenshot(
        &self,
        Parameters(params): Parameters<ScreenshotParams>,
    ) -> Result<CallToolResult, ErrorData> {
        if params.window_target().is_some() {
            let owner = self.clone();
            self.desktop_transaction
                .run(move || async move { owner.screenshot_unlocked(params).await })
                .await
        } else {
            self.screenshot_unlocked(params).await
        }
    }

    async fn screenshot_unlocked(
        &self,
        params: ScreenshotParams,
    ) -> Result<CallToolResult, ErrorData> {
        let mut target = params.window_target();
        let full_screen = params.full_screen.unwrap_or(false);
        let raise_window = params.raise_window.unwrap_or(true);
        let coordination_window_id = if full_screen {
            None
        } else {
            self.coordination_window_id(target.as_ref())
                .await
                .map_err(|error| screenshot_failure("claim_coordination", target.as_ref(), error))?
        };
        if let (Some(target), Some(window_id)) = (target.as_mut(), coordination_window_id) {
            target.pin_exact_window_id(window_id);
        }
        let _claim_guard = self
            .mutation_claim_guard(
                ClaimGuardMode::Acquire,
                coordination_window_id,
                &params.claim,
                if target.is_some() && raise_window {
                    MutationLane::PhysicalSeat
                } else {
                    MutationLane::Window
                },
            )
            .await
            .map_err(|error| screenshot_failure("claim_coordination", target.as_ref(), error))?;
        if let Some(target) = target.as_ref().filter(|_| raise_window) {
            let focus = focus_window_target(target)
                .await
                .map_err(|error| screenshot_failure("focus", Some(target), format!("{error:#}")))?;
            if !focus_satisfies_target(&focus, target) {
                return Err(screenshot_failure(
                    "focus_verification",
                    Some(target),
                    format!(
                        "requested window_id {}, focused window_id {:?}",
                        focus.requested_window.window_id,
                        focus.focused_window.as_ref().map(|window| window.window_id)
                    ),
                ));
            }
            tokio::time::sleep(Duration::from_millis(250)).await;
        }
        let window = if let Some(target) = target.as_ref() {
            let windows = if raise_window {
                registry::list_windows_with_policy(registry::WindowListPolicy::Fresh).await
            } else {
                list_windows().await
            }
            .map_err(|error| {
                screenshot_failure("window_resolution", Some(target), format!("{error:#}"))
            })?;
            Some(
                resolve_window_target(&windows, target)
                    .map_err(|error| {
                        screenshot_failure("window_resolution", Some(target), format!("{error:#}"))
                    })?
                    .clone(),
            )
        } else {
            None
        };
        let exact_capture = if full_screen {
            None
        } else if let Some(window) = window.as_ref() {
            capture_window_exact(window).await.map_err(|error| {
                screenshot_failure("exact_capture", target.as_ref(), format!("{error:#}"))
            })?
        } else {
            None
        };
        let exact = exact_capture.is_some();
        let crop = if target.is_none() || full_screen || exact {
            None
        } else {
            Some(validated_target_bounds(window.as_ref()).map_err(|error| {
                screenshot_failure("window_bounds", target.as_ref(), format!("{error:#}"))
            })?)
        };
        let raw_capture = match exact_capture {
            Some(capture) => capture,
            None => capture_screenshot_raw().await.map_err(|error| {
                screenshot_failure("capture", target.as_ref(), format!("{error:#}"))
            })?,
        };
        if !exact {
            self.cache_desktop_size(raw_capture.width, raw_capture.height);
        }
        let off_screen_note = match crop.as_ref() {
            Some(bounds) => self.off_screen_note_for_bounds(bounds).await,
            None => None,
        };
        let target_requested = target.is_some() && !full_screen && !exact;
        let screenshot_options = params.screenshot_options();
        let (capture, crop_origin) = tokio::task::spawn_blocking(move || {
            let (capture, crop_origin) =
                crop_raw_screenshot(raw_capture, crop.as_ref(), target_requested)?;
            Ok::<_, anyhow::Error>((
                prepare_screenshot_payload(capture, screenshot_options)?,
                crop_origin,
            ))
        })
        .await
        .map_err(|error| {
            screenshot_failure(
                "preparation",
                target.as_ref(),
                format!("screenshot preparation task failed: {error}"),
            )
        })?
        .map_err(|error| {
            screenshot_failure("preparation", target.as_ref(), format!("{error:#}"))
        })?;

        let mut caption = serde_json::json!({
            "width": capture.width,
            "height": capture.height,
            "coordinate_width": capture.coordinate_width,
            "coordinate_height": capture.coordinate_height,
            "scale": capture.scale,
            "resized": capture.resized,
            "bytes": capture.bytes,
            "original_bytes": capture.original_bytes,
            "max_bytes": capture.max_bytes,
            "format": capture.format,
            "quality": capture.quality,
            "source": capture.source,
            "coordinate_space": if exact || crop_origin.is_some() { "window_local" } else { "desktop" },
            "coordinate_origin_x": crop_origin.map_or(0, |(x, _)| x),
            "coordinate_origin_y": crop_origin.map_or(0, |(_, y)| y),
            "window_id": window.as_ref().map(|window| window.window_id),
            "exact_window_capture": exact,
            "cropped_to_window": exact || crop_origin.is_some(),
            "window_title": window.as_ref().and_then(|window| window.title.as_deref()),
        });
        if let Some(note) = off_screen_note {
            caption["window_off_screen"] = serde_json::json!(true);
            caption["off_screen_note"] = serde_json::json!(note);
        }
        Ok(CallToolResult::success(vec![
            Content::image(data_url_payload(&capture.data_url), capture.mime_type),
            Content::text(caption.to_string()),
        ]))
    }

    /// Lazily create the uinput absolute pointer, sizing its ABS range to the
    /// logical desktop (the portal screenshot dimensions). Returns `false` if it
    /// can't be created or is disabled via `CU_DISABLE_ABS_POINTER`.
    async fn ensure_abs_pointer(&self) -> bool {
        if PointerInputOverrides::from_env().abs_pointer_disabled {
            return false;
        }
        if self
            .abs_pointer
            .lock()
            .map(|g| g.is_some())
            .unwrap_or(false)
        {
            return true;
        }
        let Ok(cap) = capture_screenshot_raw().await else {
            return false;
        };
        self.cache_desktop_size(cap.width, cap.height);
        match tokio::task::spawn_blocking(move || {
            crate::abs_pointer::AbsPointer::create(cap.width as i32, cap.height as i32)
        })
        .await
        {
            Ok(Ok(pointer)) => {
                if let Ok(mut guard) = self.abs_pointer.lock() {
                    *guard = Some(pointer);
                    return true;
                }
                false
            }
            _ => false,
        }
    }

    /// Try a coordinate click through the absolute uinput pointer. `Some(ok)` if
    /// the backend was used; `None` to fall through to ydotool.
    async fn try_abs_click(
        &self,
        x: i32,
        y: i32,
        button: Option<&str>,
        count: u32,
        verification: Option<&PointerDispatchVerification>,
    ) -> std::result::Result<Option<bool>, String> {
        if !self.ensure_abs_pointer().await {
            return Ok(None);
        }
        let btn = crate::abs_pointer::PointerButton::from_name(button);
        let abs_pointer = Arc::clone(&self.abs_pointer);
        run_verified_pointer_dispatch(
            PointerDispatchBoundary::AbsolutePointer,
            verify_pointer_dispatch(verification, &self.accessibility_snapshots),
            async move {
                tokio::task::spawn_blocking(move || {
                    let mut guard = abs_pointer.lock().ok()?;
                    let pointer = guard.as_mut()?;
                    Some(pointer.click(x, y, btn, count).is_ok())
                })
                .await
                .ok()
                .flatten()
            },
        )
        .await
    }

    #[tool(
        name = "click",
        description = "Click an element by index or semantic selector from a specific get_app_state observation_id, or click desktop coordinate pixels from screenshot metadata. A plain left single click with no explicit window selector uses the first observed AT-SPI action directly when it is explicitly named Click; other element clicks require usable bounds and verified pointer input.",
        annotations(
            read_only_hint = false,
            destructive_hint = true,
            idempotent_hint = false,
            open_world_hint = true
        )
    )]
    async fn click(&self, Parameters(params): Parameters<ClickParams>) -> Json<ActionOutput> {
        let owner = self.clone();
        self.desktop_transaction
            .run(move || async move { owner.click_unlocked(params, ClaimGuardMode::Acquire).await })
            .await
    }

    async fn click_unlocked(
        &self,
        mut params: ClickParams,
        claim_guard_mode: ClaimGuardMode,
    ) -> Json<ActionOutput> {
        let received = Some(serde_json::json!(params.clone()));
        let click_error = |message| {
            Json(ActionOutput {
                ok: false,
                implemented: true,
                action: "click".to_string(),
                message,
                received: received.clone(),
            })
        };
        let mut target = match self.resolve_observed_click_target(&params) {
            Ok(target) => target,
            Err(message) => return click_error(message),
        };
        let coordination_window_id = match &target {
            ClickTarget::Coordinates(_, _) => match self
                .coordination_window_id(params.window_target().as_ref())
                .await
            {
                Ok(window_id) => window_id,
                Err(message) => return click_error(message),
            },
            ClickTarget::ObservedCoordinates(observed) => Some(observed.window_id),
            ClickTarget::ObservedAction(observed) => Some(observed.window_id),
        };
        if coordination_window_id.is_some() {
            params.window_id = coordination_window_id;
        }
        let _claim_guard = match self
            .mutation_claim_guard(
                claim_guard_mode,
                coordination_window_id,
                &params.claim,
                if matches!(&target, ClickTarget::ObservedAction(_)) {
                    MutationLane::Window
                } else {
                    MutationLane::PhysicalSeat
                },
            )
            .await
        {
            Ok(guard) => guard,
            Err(message) => return click_error(message),
        };
        if let ClickTarget::ObservedAction(observed) = &target {
            if let Err(message) = self.verify_observed_click_action_freshness(observed) {
                return click_error(message);
            }
            return match perform_action_by_identity(&observed.object_ref, &observed.action_identity)
                .await
            {
                Ok(invocation) => Json(ActionOutput {
                    ok: invocation.ok,
                    implemented: true,
                    action: "click".to_string(),
                    message: if invocation.ok {
                        "Invoked the observation-bound AT-SPI Click action.".to_string()
                    } else {
                        "The observation-bound AT-SPI Click action returned false.".to_string()
                    },
                    received,
                }),
                Err(error) => click_error(error.to_string()),
            };
        }
        let element_targeted = matches!(target, ClickTarget::ObservedCoordinates(_));
        // Raise the target window first (if specified) so the click lands on the
        // intended app rather than whatever is stacked on top at that pixel.
        let mut window_target = params.window_target();
        let mut pointer_verification = None;
        if let ClickTarget::ObservedCoordinates(observed) = &target {
            let point = observed.point;
            let prepared = match self
                .prepare_observed_click_target(observed, window_target)
                .await
            {
                Ok(prepared) => prepared,
                Err(message) => return click_error(message),
            };
            window_target = Some(prepared.0);
            pointer_verification = Some(prepared.1);
            target = ClickTarget::Coordinates(point.0, point.1);
        }
        if params.relative == Some(true) && window_target.is_none() {
            return Json(ActionOutput {
                ok: false,
                implemented: true,
                action: "click".to_string(),
                message: "Relative coordinate clicks require a window target.".to_string(),
                received,
            });
        }
        if let Some(focus_target) = window_target {
            let focus = match self.focus_target_for_input(&focus_target).await {
                Ok(focus) => focus,
                Err(message) => {
                    return Json(ActionOutput {
                        ok: false,
                        implemented: true,
                        action: "click".to_string(),
                        message,
                        received,
                    });
                }
            };
            if !element_targeted {
                pointer_verification =
                    pointer_dispatch_verification(&focus_target, params.relative, focus.as_ref());
            }
            tokio::time::sleep(Duration::from_millis(120)).await;
            // Window-relative coordinates: translate by the window's top-left so
            // the agent can click the pixel it saw in a window-cropped screenshot.
            if params.relative == Some(true) {
                let Some(focus) = focus.as_ref() else {
                    return Json(ActionOutput {
                        ok: false,
                        implemented: true,
                        action: "click".to_string(),
                        message: "Relative coordinate clicks require verified target-window focus."
                            .to_string(),
                        received,
                    });
                };
                if let Err(message) = apply_window_relative_click_coordinates(&mut params, focus) {
                    return Json(ActionOutput {
                        ok: false,
                        implemented: true,
                        action: "click".to_string(),
                        message,
                        received,
                    });
                }
                let point = params.x.zip(params.y).expect("validated coordinates");
                target = ClickTarget::Coordinates(point.0, point.1);
            }
            let claim_error = match &target {
                ClickTarget::Coordinates(x, y) => {
                    validate_claimed_window_point(&params.claim, focus.as_ref(), (*x, *y), "click")
                        .err()
                }
                ClickTarget::ObservedCoordinates(_) | ClickTarget::ObservedAction(_) => None,
            };
            if let Some(message) = claim_error {
                return click_error(message);
            }
        }
        let ClickTarget::Coordinates(x, y) = target else {
            unreachable!("click target must resolve to coordinates");
        };
        if let Err(message) = self.validate_capture_space_point(x, y).await {
            return Json(ActionOutput {
                ok: false,
                implemented: true,
                action: "click".to_string(),
                message,
                received,
            });
        }
        let button = mouse_button_code(params.button.as_deref());
        let click_count = params.click_count.unwrap_or(1).clamp(1, 10).to_string();
        // Preferred backend: the uinput absolute pointer. Unlike ydotool's
        // relative-only device (faked `--absolute` via pin-to-corner + relative
        // move, which acceleration + fractional scaling distort) and unlike the
        // portal (per-monitor coordinate scaling + an approval dialog), the
        // absolute pointer lands exactly at the screenshot pixel.
        match self
            .try_abs_click(
                x,
                y,
                params.button.as_deref(),
                params.click_count.unwrap_or(1).clamp(1, 10),
                pointer_verification.as_ref(),
            )
            .await
        {
            Ok(Some(true)) => {
                return Json(ActionOutput {
                    ok: true,
                    implemented: true,
                    action: "click".to_string(),
                    message: "Action sent through the uinput absolute pointer.".to_string(),
                    received,
                });
            }
            Err(message) => return click_error(message),
            Ok(Some(false) | None) => {}
        }
        if !self.can_fallback_to_ydotool_for_coordinate_action() {
            return Json(portal_coordinate_input_unavailable("click", received));
        }
        let sequence = [
            absolute_mousemove_args(x, y),
            vec![
                "click".to_string(),
                "--repeat".to_string(),
                click_count,
                button,
            ],
        ];
        let result = match run_verified_pointer_dispatch(
            PointerDispatchBoundary::Ydotool,
            verify_pointer_dispatch(pointer_verification.as_ref(), &self.accessibility_snapshots),
            run_ydotool_sequence(&sequence),
        )
        .await
        {
            Ok(result) => result,
            Err(message) => return click_error(message),
        };
        Json(action_result("click", result, received))
    }

    #[tool(
        name = "run_action_batch",
        description = "Run a validated, ordered, fail-fast batch of common input actions against one exact window_id. Supports up to eight press_key/type_text actions and, optionally, one leading click. The complete batch is validated before any input is sent. Every action re-verifies exact target focus immediately before input; bounds-free semantic fallback clicks invoke AT-SPI directly, while pointer clicks wait for the window stack to settle.",
        annotations(
            read_only_hint = false,
            destructive_hint = true,
            idempotent_hint = false,
            open_world_hint = true
        )
    )]
    async fn run_action_batch(
        &self,
        Parameters(params): Parameters<ActionBatchParams>,
    ) -> Json<ActionBatchOutput> {
        let owner = self.clone();
        self.desktop_transaction
            .run(move || async move { owner.run_action_batch_unlocked(params).await })
            .await
    }

    async fn run_action_batch_unlocked(
        &self,
        params: ActionBatchParams,
    ) -> Json<ActionBatchOutput> {
        if let Err(error) = self.validate_action_batch(&params) {
            return Json(ActionBatchOutput::validation_error(error));
        }
        let _claim_guard = match self
            .mutation_claim_guard(
                ClaimGuardMode::Acquire,
                Some(params.window_id),
                &params.claim,
                MutationLane::PhysicalSeat,
            )
            .await
        {
            Ok(guard) => guard,
            Err(error) => return Json(ActionBatchOutput::validation_error(error)),
        };

        Json(self.execute_validated_action_batch_unlocked(params).await)
    }

    #[tool(
        name = "run_action_batch_and_observe",
        description = "Run the same validated, ordered, fail-fast input batch as run_action_batch, then return a window-scoped adaptive observation in the same call. Validation failures do not capture state; once execution starts, post-action state is captured even if an action fails so the caller can recover.",
        output_schema = rmcp::handler::server::tool::schema_for_type::<ActionBatchAndObserveOutput>(),
        annotations(
            read_only_hint = false,
            destructive_hint = true,
            idempotent_hint = false,
            open_world_hint = true
        )
    )]
    async fn run_action_batch_and_observe(
        &self,
        Parameters(params): Parameters<ActionBatchAndObserveParams>,
    ) -> Result<CallToolResult, ErrorData> {
        let owner = self.clone();
        self.desktop_transaction
            .run(move || async move { owner.run_action_batch_and_observe_unlocked(params).await })
            .await
    }

    async fn run_action_batch_and_observe_unlocked(
        &self,
        params: ActionBatchAndObserveParams,
    ) -> Result<CallToolResult, ErrorData> {
        if let Err(error) = self.validate_action_batch(&params.batch) {
            return action_batch_and_observation_tool_result(
                ActionBatchOutput::validation_error(error),
                PostActionObservationResult::NotAttempted,
            );
        }

        let window_id = params.batch.window_id;
        let claim = params.batch.claim.clone();
        let _claim_guard = self
            .mutation_claim_guard(
                ClaimGuardMode::Acquire,
                Some(window_id),
                &claim,
                MutationLane::PhysicalSeat,
            )
            .await
            .map_err(|error| ErrorData::invalid_params(error, None))?;
        let batch = self
            .execute_validated_action_batch_unlocked(params.batch)
            .await;
        let observation = match self
            .get_app_state_unlocked(
                params
                    .observation
                    .into_get_app_state_params(window_id, claim),
                ClaimGuardMode::AlreadyHeld,
            )
            .await
        {
            Ok(observation) => PostActionObservationResult::Completed(observation),
            Err(error) => PostActionObservationResult::Failed(error),
        };
        action_batch_and_observation_tool_result(batch, observation)
    }

    #[tool(
        name = "perform_action",
        description = "Invoke an accessibility action exposed by an element selected by index, identifier, or semantic selector from a specific get_app_state observation_id. The action is also resolved from that observation and invoked by a field-preserving name/description fingerprint. Defaults to the first observed action unless action is provided.",
        annotations(
            read_only_hint = false,
            destructive_hint = true,
            idempotent_hint = false,
            open_world_hint = true
        )
    )]
    async fn perform_action(
        &self,
        Parameters(params): Parameters<ActionParams>,
    ) -> Json<ActionOutput> {
        let owner = self.clone();
        self.desktop_transaction
            .run(move || async move {
                owner
                    .perform_action_unlocked(params, ClaimGuardMode::Acquire)
                    .await
            })
            .await
    }

    async fn perform_action_unlocked(
        &self,
        params: ActionParams,
        claim_guard_mode: ClaimGuardMode,
    ) -> Json<ActionOutput> {
        let received = Some(serde_json::json!(params.clone()));
        let coordination_window_id =
            match self.observation_window_id(Some(params.observation_id.as_str())) {
                Ok(window_id) => window_id,
                Err(message) => return action_error("perform_action", message, received),
            };
        let _claim_guard = match self
            .mutation_claim_guard(
                claim_guard_mode,
                coordination_window_id,
                &params.claim,
                MutationLane::Window,
            )
            .await
        {
            Ok(guard) => guard,
            Err(message) => return action_error("perform_action", message, received),
        };
        self.perform_element_action(&params).await
    }

    #[tool(
        name = "set_value",
        description = "Set the value of a settable accessibility element selected by index, identifier, or semantic selector from a specific get_app_state observation_id.",
        annotations(
            read_only_hint = false,
            destructive_hint = true,
            idempotent_hint = false,
            open_world_hint = true
        )
    )]
    async fn set_value(
        &self,
        Parameters(params): Parameters<SetValueParams>,
    ) -> Json<ActionOutput> {
        let owner = self.clone();
        self.desktop_transaction
            .run(move || async move {
                owner
                    .set_value_unlocked(params, ClaimGuardMode::Acquire)
                    .await
            })
            .await
    }

    async fn set_value_unlocked(
        &self,
        params: SetValueParams,
        claim_guard_mode: ClaimGuardMode,
    ) -> Json<ActionOutput> {
        let received = Some(serde_json::json!(params.clone()));
        let coordination_window_id =
            match self.observation_window_id(Some(params.observation_id.as_str())) {
                Ok(window_id) => window_id,
                Err(message) => return action_error("set_value", message, received),
            };
        let _claim_guard = match self
            .mutation_claim_guard(
                claim_guard_mode,
                coordination_window_id,
                &params.claim,
                MutationLane::Window,
            )
            .await
        {
            Ok(guard) => guard,
            Err(message) => return action_error("set_value", message, received),
        };
        let object_ref = match self.resolve_object_ref(
            Some(params.observation_id.as_str()),
            params.element_index,
            params.element_identifier.as_deref(),
            &params.selector(),
            ElementResolvePurpose::SetValue,
        ) {
            Ok(object_ref) => object_ref,
            Err(message) => {
                return Json(ActionOutput {
                    ok: false,
                    implemented: true,
                    action: "set_value".to_string(),
                    message,
                    received,
                });
            }
        };

        match set_element_value(&object_ref, &params.value).await {
            Ok(ValueSetInvocation::Numeric { value }) => Json(ActionOutput {
                ok: true,
                implemented: true,
                action: "set_value".to_string(),
                message: format!("AT-SPI numeric value set to {value}."),
                received,
            }),
            Ok(ValueSetInvocation::EditableText) => Json(ActionOutput {
                ok: true,
                implemented: true,
                action: "set_value".to_string(),
                message: "AT-SPI editable text contents set.".to_string(),
                received,
            }),
            Err(error) => Json(ActionOutput {
                ok: false,
                implemented: true,
                action: "set_value".to_string(),
                message: error.to_string(),
                received,
            }),
        }
    }

    #[tool(
        name = "scroll",
        description = "Scroll an element from a specific get_app_state observation_id, desktop coordinates, the targeted window centre, or the current pointer position.",
        annotations(
            read_only_hint = false,
            destructive_hint = false,
            idempotent_hint = false,
            open_world_hint = true
        )
    )]
    async fn scroll(&self, Parameters(params): Parameters<ScrollParams>) -> Json<ActionOutput> {
        let owner = self.clone();
        self.desktop_transaction
            .run(
                move || async move { owner.scroll_unlocked(params, ClaimGuardMode::Acquire).await },
            )
            .await
    }

    async fn scroll_unlocked(
        &self,
        mut params: ScrollParams,
        claim_guard_mode: ClaimGuardMode,
    ) -> Json<ActionOutput> {
        let received = Some(serde_json::json!(params.clone()));
        let scroll_error = |message| {
            Json(ActionOutput {
                ok: false,
                implemented: true,
                action: "scroll".to_string(),
                message,
                received: received.clone(),
            })
        };
        let units = ((params.pages.unwrap_or(1.0).abs().max(0.1) * 5.0).round() as i32).max(1);
        let mut explicit_window_target = params.window_target();
        let observed_target = match resolve_observed_scroll_target(
            &self.accessibility_snapshots,
            ScrollTargetRequest {
                observation_id: params.observation_id.as_deref(),
                element_index: params.element_index,
                x: params.x,
                y: params.y,
                relative: params.relative == Some(true),
                window_target: explicit_window_target.clone(),
            },
        ) {
            Ok(target) => target,
            Err(message) => return scroll_error(message),
        };
        let coordination_window_id = if let Some(target) = &observed_target {
            Some(target.window_id())
        } else {
            match self
                .coordination_window_id(explicit_window_target.as_ref())
                .await
            {
                Ok(window_id) => window_id,
                Err(message) => return scroll_error(message),
            }
        };
        if let (Some(window_id), Some(target)) =
            (coordination_window_id, explicit_window_target.as_mut())
        {
            target.window_id = Some(window_id);
        }
        let _claim_guard = match self
            .mutation_claim_guard(
                claim_guard_mode,
                coordination_window_id,
                &params.claim,
                MutationLane::PhysicalSeat,
            )
            .await
        {
            Ok(guard) => guard,
            Err(message) => return scroll_error(message),
        };
        // Raise/focus the target window first (parity with click) so wheel
        // events land on the intended app.
        let (window_target, pointer_verification) = if let Some(target) = &observed_target {
            match target.prepare().await {
                Ok((window_target, verification)) => (Some(window_target), Some(verification)),
                Err(message) => return scroll_error(message),
            }
        } else {
            (explicit_window_target, None)
        };
        if params.relative == Some(true) && window_target.is_none() {
            return Json(ActionOutput {
                ok: false,
                implemented: true,
                action: "scroll".to_string(),
                message: "Relative scroll coordinates require a window target.".to_string(),
                received,
            });
        }
        if let Some(target) = window_target {
            let focus = match self.focus_target_for_input(&target).await {
                Ok(focus) => focus,
                Err(message) => {
                    return Json(ActionOutput {
                        ok: false,
                        implemented: true,
                        action: "scroll".to_string(),
                        message,
                        received,
                    });
                }
            };
            tokio::time::sleep(Duration::from_millis(120)).await;
            if params.relative == Some(true) {
                let Some(focus) = focus.as_ref() else {
                    return Json(ActionOutput {
                        ok: false,
                        implemented: true,
                        action: "scroll".to_string(),
                        message:
                            "Relative scroll coordinates require verified target-window focus."
                                .to_string(),
                        received,
                    });
                };
                if let Err(message) = apply_window_relative_scroll_coordinates(&mut params, focus) {
                    return Json(ActionOutput {
                        ok: false,
                        implemented: true,
                        action: "scroll".to_string(),
                        message,
                        received,
                    });
                }
            } else if params.x.is_none() && params.y.is_none() && params.element_index.is_none() {
                // A window target without a point would otherwise scroll
                // whatever happens to sit under the pointer: focusing does not
                // move the cursor, and the wheel path never repositions it.
                // Default to the centre of the resolved target window.
                let Some(focus) = focus.as_ref() else {
                    return Json(ActionOutput {
                        ok: false,
                        implemented: true,
                        action: "scroll".to_string(),
                        message: "Window-targeted scroll requires verified target-window focus."
                            .to_string(),
                        received,
                    });
                };
                if let Err(message) = apply_window_center_scroll_point(&mut params, focus) {
                    return Json(ActionOutput {
                        ok: false,
                        implemented: true,
                        action: "scroll".to_string(),
                        message,
                        received,
                    });
                }
            }
            let point = observed_target
                .as_ref()
                .map_or_else(|| params.x.zip(params.y), |target| Some(target.point()));
            let claim_error = point.and_then(|point| {
                validate_claimed_window_point(&params.claim, focus.as_ref(), point, "scroll").err()
            });
            if let Some(message) = claim_error {
                return scroll_error(message);
            }
        }
        let target_point = observed_target
            .as_ref()
            .map_or_else(|| params.x.zip(params.y), |target| Some(target.point()));
        let direction = match params.direction.to_ascii_lowercase().as_str() {
            "up" => ScrollDirection::Up,
            "down" => ScrollDirection::Down,
            "left" => ScrollDirection::Left,
            "right" => ScrollDirection::Right,
            _ => {
                return Json(ActionOutput {
                    ok: false,
                    implemented: true,
                    action: "scroll".to_string(),
                    message: "Unsupported scroll direction; expected up, down, left, or right."
                        .to_string(),
                    received,
                });
            }
        };
        let coordinate_error = match target_point {
            Some((x, y)) => self.validate_capture_space_point(x, y).await.err(),
            None => None,
        };
        if let Some(message) = coordinate_error {
            return Json(ActionOutput {
                ok: false,
                implemented: true,
                action: "scroll".to_string(),
                message,
                received,
            });
        }

        if target_point.is_some() && !self.can_fallback_to_ydotool_for_coordinate_action() {
            return Json(portal_coordinate_input_unavailable("scroll", received));
        }
        let portal_session = if target_point.is_none() {
            self.portal_pointer_session_for_action().await
        } else {
            None
        };
        if let Some(session) = portal_session {
            let session = session.clone();
            let result = self
                .run_portal_pointer_action(async move {
                    portal_scroll(&session, direction, units).await
                })
                .await;
            match result {
                Ok(()) => {
                    return Json(ActionOutput {
                        ok: true,
                        implemented: true,
                        action: "scroll".to_string(),
                        message: "Action sent through the remote desktop portal.".to_string(),
                        received,
                    });
                }
                Err(error) if error.can_fallback_to_ydotool() => {}
                Err(error) => {
                    return Json(portal_action_delivery_failure("scroll", &error, received));
                }
            }
        }
        let (dx, dy) = match params.direction.to_ascii_lowercase().as_str() {
            "up" => (0, units),
            "down" => (0, -units),
            "left" => (units, 0),
            "right" => (-units, 0),
            _ => {
                return Json(ActionOutput {
                    ok: false,
                    implemented: true,
                    action: "scroll".to_string(),
                    message: "Unsupported scroll direction; expected up, down, left, or right."
                        .to_string(),
                    received,
                });
            }
        };
        let mut sequence = Vec::new();
        if let Some((x, y)) = target_point {
            sequence.push(absolute_mousemove_args(x, y));
        }
        sequence.push(wheel_mousemove_args(dx, dy));
        let result = match run_verified_pointer_dispatch(
            PointerDispatchBoundary::Ydotool,
            verify_pointer_dispatch(pointer_verification.as_ref(), &self.accessibility_snapshots),
            run_ydotool_sequence(&sequence),
        )
        .await
        {
            Ok(result) => result,
            Err(message) => return scroll_error(message),
        };
        Json(action_result("scroll", result, received))
    }

    #[tool(
        name = "drag",
        description = "Drag from one point to another using pixel coordinates.",
        annotations(
            read_only_hint = false,
            destructive_hint = true,
            idempotent_hint = false,
            open_world_hint = true
        )
    )]
    async fn drag(&self, Parameters(params): Parameters<DragParams>) -> Json<ActionOutput> {
        let owner = self.clone();
        self.desktop_transaction
            .run(move || async move { owner.drag_unlocked(params, ClaimGuardMode::Acquire).await })
            .await
    }

    async fn drag_unlocked(
        &self,
        params: DragParams,
        claim_guard_mode: ClaimGuardMode,
    ) -> Json<ActionOutput> {
        let received = Some(serde_json::json!(params));
        let mut window_target = params.window_target();
        let coordination_window_id = match self.coordination_window_id(Some(&window_target)).await {
            Ok(window_id) => window_id,
            Err(message) => return action_error("drag", message, received),
        };
        window_target.window_id = coordination_window_id;
        let _claim_guard = match self
            .mutation_claim_guard(
                claim_guard_mode,
                coordination_window_id,
                &params.claim,
                MutationLane::PhysicalSeat,
            )
            .await
        {
            Ok(guard) => guard,
            Err(message) => return action_error("drag", message, received),
        };
        let focus_result = if window_target.has_target() {
            self.focus_target_for_input(&window_target).await
        } else {
            Ok(None)
        };
        let focus = match focus_result {
            Ok(focus) => focus,
            Err(message) => return action_error("drag", message, received),
        };
        for (label, x, y) in [
            ("start", params.start_x, params.start_y),
            ("end", params.end_x, params.end_y),
        ] {
            if let Err(message) =
                validate_claimed_window_point(&params.claim, focus.as_ref(), (x, y), "drag")
            {
                return action_error("drag", message, received);
            }
            if let Err(message) = self.validate_capture_space_point(x, y).await {
                return Json(ActionOutput {
                    ok: false,
                    implemented: true,
                    action: "drag".to_string(),
                    message: format!("Invalid drag {label} point: {message}"),
                    received,
                });
            }
        }
        // Preferred backend: the uinput absolute pointer (accurate landing).
        if self.ensure_abs_pointer().await {
            let abs_pointer = Arc::clone(&self.abs_pointer);
            let dragged = tokio::task::spawn_blocking(move || {
                if let Ok(mut guard) = abs_pointer.lock() {
                    guard.as_mut().map(|p| {
                        p.drag(
                            (params.start_x, params.start_y),
                            (params.end_x, params.end_y),
                            crate::abs_pointer::PointerButton::Left,
                        )
                        .is_ok()
                    })
                } else {
                    None
                }
            })
            .await
            .ok()
            .flatten();
            if dragged == Some(true) {
                return Json(ActionOutput {
                    ok: true,
                    implemented: true,
                    action: "drag".to_string(),
                    message: "Action sent through the uinput absolute pointer.".to_string(),
                    received,
                });
            }
        }
        if !self.can_fallback_to_ydotool_for_coordinate_action() {
            return Json(portal_coordinate_input_unavailable("drag", received));
        }
        let result = run_ydotool_sequence(&[
            absolute_mousemove_args(params.start_x, params.start_y),
            vec!["click".to_string(), "0x40".to_string()],
            absolute_mousemove_args(params.end_x, params.end_y),
            vec!["click".to_string(), "0x80".to_string()],
        ])
        .await;
        Json(action_result("drag", result, received))
    }

    #[tool(
        name = "press_key",
        description = "Press a key or key-combination on the keyboard, optionally after focusing a target window or terminal selector. Key grammar (case-insensitive; hyphens/spaces ignored): combos join with '+', e.g. Ctrl+L or Ctrl+Shift+T. Modifiers: ctrl/control, alt/option, shift, meta/super/cmd/command. Named keys: enter/return, escape/esc, tab, backspace, delete/del, space, home, end, pageup, pagedown, arrowleft/left, arrowright/right, arrowup/up, arrowdown/down, f1-f12. Plus single US letters a-z and digits 0-9. Anything else returns an error (never silently dropped). Note: compositor-level shortcuts (e.g. Super+Up) may be consumed by GNOME before reaching the app.",
        annotations(
            read_only_hint = false,
            destructive_hint = true,
            idempotent_hint = false,
            open_world_hint = true
        )
    )]
    async fn press_key(
        &self,
        Parameters(params): Parameters<PressKeyParams>,
    ) -> Json<ActionOutput> {
        let owner = self.clone();
        self.desktop_transaction
            .run(move || async move {
                owner
                    .press_key_unlocked(params, ClaimGuardMode::Acquire)
                    .await
            })
            .await
    }

    async fn press_key_unlocked(
        &self,
        params: PressKeyParams,
        claim_guard_mode: ClaimGuardMode,
    ) -> Json<ActionOutput> {
        let received = Some(serde_json::json!(params.clone()));
        let mut window_target = params.window_target();
        let coordination_window_id = match self.coordination_window_id(Some(&window_target)).await {
            Ok(window_id) => window_id,
            Err(message) => return action_error("press_key", message, received),
        };
        window_target.window_id = coordination_window_id;
        let _claim_guard = match self
            .mutation_claim_guard(
                claim_guard_mode,
                coordination_window_id,
                &params.claim,
                MutationLane::PhysicalSeat,
            )
            .await
        {
            Ok(guard) => guard,
            Err(message) => return action_error("press_key", message, received),
        };
        let focus = match self.focus_target_for_input(&window_target).await {
            Ok(focus) => focus,
            Err(message) => {
                return Json(ActionOutput {
                    ok: false,
                    implemented: true,
                    action: "press_key".to_string(),
                    message,
                    received,
                });
            }
        };
        let Some(key_events) = key_sequence(&params.key) else {
            return Json(ActionOutput {
                ok: false,
                implemented: true,
                action: "press_key".to_string(),
                message: "Unsupported key. Use names like Enter, Escape, Tab, ArrowLeft, Super, Ctrl+L, or a single US keyboard letter/digit.".to_string(),
                received,
            });
        };
        let mut args = vec!["key".to_string()];
        args.extend(key_events);
        let result = run_ydotool(&args).await.map(|output| vec![output]);
        let mut output = action_result_with_focus("press_key", result, received, focus.clone());
        if output.ok && focus.is_some() {
            let notes = self.input_landing_notes(focus.as_ref(), false).await;
            output = with_notes(output, notes);
        }
        Json(output)
    }

    #[tool(
        name = "type_text",
        description = "Type literal text using keyboard input, optionally after focusing a target window or terminal selector.",
        annotations(
            read_only_hint = false,
            destructive_hint = true,
            idempotent_hint = false,
            open_world_hint = true
        )
    )]
    async fn type_text(
        &self,
        Parameters(params): Parameters<TypeTextParams>,
    ) -> Json<ActionOutput> {
        let owner = self.clone();
        self.desktop_transaction
            .run(move || async move {
                owner
                    .type_text_unlocked(params, ClaimGuardMode::Acquire)
                    .await
            })
            .await
    }

    async fn type_text_unlocked(
        &self,
        params: TypeTextParams,
        claim_guard_mode: ClaimGuardMode,
    ) -> Json<ActionOutput> {
        let received = Some(serde_json::json!(params.clone()));
        let mut window_target = params.window_target();
        let coordination_window_id = match self.coordination_window_id(Some(&window_target)).await {
            Ok(window_id) => window_id,
            Err(message) => return action_error("type_text", message, received),
        };
        window_target.window_id = coordination_window_id;
        let _claim_guard = match self
            .mutation_claim_guard(
                claim_guard_mode,
                coordination_window_id,
                &params.claim,
                MutationLane::PhysicalSeat,
            )
            .await
        {
            Ok(guard) => guard,
            Err(message) => return action_error("type_text", message, received),
        };
        let focus = match self.focus_target_for_input(&window_target).await {
            Ok(focus) => focus,
            Err(message) => {
                return Json(ActionOutput {
                    ok: false,
                    implemented: true,
                    action: "type_text".to_string(),
                    message,
                    received,
                });
            }
        };
        if self.should_prefer_kde_clipboard_text_backend() {
            match self.ensure_portal_keyboard_session().await {
                Ok(Some(session)) => {
                    let _clipboard_guard = self.kde_clipboard_lock.lock().await;
                    match run_kde_clipboard_paste_text(&session, &params.text).await {
                        Ok(message) => {
                            let notes = self.input_landing_notes(focus.as_ref(), true).await;
                            return Json(with_notes(
                                successful_action_with_focus(
                                    "type_text",
                                    &message,
                                    received,
                                    focus,
                                ),
                                notes,
                            ));
                        }
                        Err(error) => {
                            if error.clear_portal_keyboard_session {
                                self.clear_portal_keyboard_session();
                            }
                            if !error.can_fallback_to_ydotool {
                                return Json(action_result_with_focus(
                                    "type_text",
                                    Err(error.message),
                                    received,
                                    focus,
                                ));
                            }
                        }
                    }
                }
                Ok(None) => {}
                Err(_) => {}
            }
        }
        if self.should_prefer_portal_keyboard_backend() {
            if let Ok(keysyms) = keysyms_for_text(&params.text) {
                match self.ensure_portal_keyboard_session().await {
                    Ok(Some(session)) => match type_text_with_keysyms(&session, &keysyms).await {
                        Ok(()) => {
                            let notes = self.input_landing_notes(focus.as_ref(), true).await;
                            return Json(with_notes(
                                successful_action_with_focus(
                                    "type_text",
                                    "Action sent through the remote desktop portal.",
                                    received,
                                    focus,
                                ),
                                notes,
                            ));
                        }
                        Err(error) => {
                            self.clear_portal_keyboard_session();
                            return Json(action_result_with_focus(
                                "type_text",
                                Err(format!("{error:#}")),
                                received,
                                focus,
                            ));
                        }
                    },
                    Ok(None) => {}
                    Err(_) => {}
                }
            }
        }
        let result = run_ydotool_type_text(&params.text)
            .await
            .map(|output| vec![output]);
        let mut output = action_result_with_focus("type_text", result, received, focus.clone());
        if output.ok && focus.is_some() {
            let notes = self.input_landing_notes(focus.as_ref(), true).await;
            output = with_notes(output, notes);
        }
        Json(output)
    }

    #[tool(
        name = "move_window",
        description = "Move a window to a new desktop position (frame top-left in desktop coordinates). Useful to recover windows that are partially off-screen. Works through the computer-use-linux GNOME Shell extension or a generic X11/EWMH window manager (wmctrl).",
        annotations(
            read_only_hint = false,
            destructive_hint = false,
            idempotent_hint = true,
            open_world_hint = true
        )
    )]
    async fn move_window(
        &self,
        Parameters(params): Parameters<MoveWindowParams>,
    ) -> Json<WindowGeometryOutput> {
        let owner = self.clone();
        self.desktop_transaction
            .run(move || async move { owner.move_window_unlocked(params).await })
            .await
    }

    async fn move_window_unlocked(&self, params: MoveWindowParams) -> Json<WindowGeometryOutput> {
        let received = Some(serde_json::json!(params.clone()));
        let target = params.target.clone().into_target();
        self.window_geometry_op(received, &target, |window| async move {
            registry::move_window(&window, params.x, params.y).await
        })
        .await
    }

    #[tool(
        name = "resize_window",
        description = "Resize a window to a new frame width/height in desktop pixels, unmaximizing it first if needed. Useful to fit a window fully on-screen. Works through the computer-use-linux GNOME Shell extension or a generic X11/EWMH window manager (wmctrl).",
        annotations(
            read_only_hint = false,
            destructive_hint = false,
            idempotent_hint = true,
            open_world_hint = true
        )
    )]
    async fn resize_window(
        &self,
        Parameters(params): Parameters<ResizeWindowParams>,
    ) -> Json<WindowGeometryOutput> {
        let owner = self.clone();
        self.desktop_transaction
            .run(move || async move { owner.resize_window_unlocked(params).await })
            .await
    }

    async fn resize_window_unlocked(
        &self,
        params: ResizeWindowParams,
    ) -> Json<WindowGeometryOutput> {
        let received = Some(serde_json::json!(params.clone()));
        let target = params.target.clone().into_target();
        self.window_geometry_op(received, &target, |window| async move {
            registry::resize_window(&window, params.width, params.height).await
        })
        .await
    }
}

fn app_state_tool_result(
    mut output: GetAppStateOutput,
    screenshots: &[&ScreenshotCapture],
) -> Result<CallToolResult, ErrorData> {
    let screenshot_failed = output.screenshot_error.is_some();
    if screenshot_failed {
        output.screenshot = None;
        output.screenshot_regions.clear();
    }
    let value = serde_json::to_value(output).map_err(|error| {
        ErrorData::internal_error(format!("failed to serialize app state: {error}"), None)
    })?;
    let mut result = CallToolResult::structured(value);
    if !screenshot_failed {
        for screenshot in screenshots {
            result.content.push(Content::image(
                data_url_payload(&screenshot.data_url),
                screenshot.mime_type.clone(),
            ));
        }
    }
    Ok(result)
}

fn action_batch_and_observation_tool_result(
    batch: ActionBatchOutput,
    observation_result: PostActionObservationResult,
) -> Result<CallToolResult, ErrorData> {
    let (mut observation, observation_error, mut images) = match observation_result {
        PostActionObservationResult::Completed(result) => {
            let observation = result.structured_content.ok_or_else(|| {
                ErrorData::internal_error(
                    "get_app_state returned no structured post-action observation",
                    None,
                )
            })?;
            let images = result
                .content
                .into_iter()
                .filter(|content| content.raw.as_image().is_some())
                .collect::<Vec<_>>();
            (Some(observation), None, images)
        }
        PostActionObservationResult::Failed(error) => (
            None,
            Some(bounded_action_result_message(error.message.as_ref())),
            Vec::new(),
        ),
        PostActionObservationResult::NotAttempted => (None, None, Vec::new()),
    };
    if let Some(observation) = observation.as_mut() {
        bound_model_visible_json_strings(observation);
    }
    let mut value = serde_json::to_value(ActionBatchAndObserveOutput {
        batch,
        observation,
        observation_error,
    })
    .map_err(|error| {
        ErrorData::internal_error(
            format!("failed to serialize batch and observation result: {error}"),
            None,
        )
    })?;
    const MAX_STRUCTURED_BYTES: usize = 8 * 1024;
    let mut tree_truncated = false;
    loop {
        if tree_truncated {
            value["observation_error"] = serde_json::Value::String(
                "The post-action accessibility tree was truncated to keep the combined result bounded. Call get_app_state if more nodes are needed."
                    .to_string(),
            );
        }
        if serde_json::to_vec(&value)
            .is_ok_and(|serialized| serialized.len() <= MAX_STRUCTURED_BYTES)
        {
            break;
        }
        let Some(tree) = value
            .get_mut("observation")
            .and_then(|observation| observation.get_mut("accessibility_tree"))
            .and_then(serde_json::Value::as_array_mut)
            .filter(|tree| !tree.is_empty())
        else {
            value["observation"] = serde_json::Value::Null;
            value["observation_error"] = serde_json::Value::String(
                "The post-action observation exceeded the combined result limit. Call get_app_state to retrieve current state."
                    .to_string(),
            );
            images.clear();
            break;
        };
        tree.truncate(tree.len() / 2);
        tree_truncated = true;
    }
    let mut result = CallToolResult::structured(value);
    result.content.extend(images);
    Ok(result)
}

fn bound_model_visible_json_strings(value: &mut serde_json::Value) {
    match value {
        serde_json::Value::String(text) => *text = bounded_action_result_message(text),
        serde_json::Value::Array(values) => {
            values.iter_mut().for_each(bound_model_visible_json_strings);
        }
        serde_json::Value::Object(values) => {
            values
                .values_mut()
                .for_each(bound_model_visible_json_strings);
        }
        serde_json::Value::Null | serde_json::Value::Bool(_) | serde_json::Value::Number(_) => {}
    }
}

enum PostActionObservationResult {
    NotAttempted,
    Completed(CallToolResult),
    Failed(ErrorData),
}

#[tool_handler(
    router = self.mcp_tool_router(),
    name = "computer-use-linux",
    // NOTE: keep in lockstep with Cargo.toml + package.json on every release.
    // The rmcp tool_handler macro only accepts a string literal here, so this
    // can't be env!("CARGO_PKG_VERSION"); the MCP safety check (CI) fails the
    // build if it drifts from the Cargo version.
    version = "0.5.0",
    instructions = "Begin every turn that uses Computer Use by calling get_app_state. If diagnostics report disabled GNOME accessibility, call setup_accessibility before asking the user to retry. Use list_windows/focused_window before targeted keyboard input. If diagnostics report windowing.can_list_windows=false on GNOME, call setup_window_targeting to install the optional GNOME Shell extension backend, then ask the user to log out and back in if the setup report says a shell reload is required. This Linux backend can capture size-bounded screenshots through GNOME Shell or XDG Desktop Portal, read AT-SPI trees with action/value metadata, invoke native AT-SPI actions, set AT-SPI values or editable text, list/focus compositor windows through registered Linux window backends when the session permits it, attach best-effort terminal tty/process metadata to terminal windows, send coordinate click/drag through absolute uinput or ydotool, send targeted scroll through ydotool, send untargeted relative scroll and layout-safe key input through the Wayland remote desktop portal, and send literal type_text through KDE clipboard integration on Plasma Wayland. The portal is never used for screenshot-pixel coordinates because its API defines no safe screenshot-to-pointer transform; click/drag are refused when neither absolute uinput nor an allowed working ydotool backend is available, and targeted scroll is refused without allowed ydotool. Screenshot results include width/height for the returned image plus coordinate_width/coordinate_height and scale for desktop coordinate conversion; request more detail with max_width, max_height, max_bytes, format=jpeg, quality, or a smaller target/crop instead of relying on unbounded screenshots. Tools with readOnlyHint=false may mutate local desktop or application state; hosts should require approval for actions that can submit, delete, send, purchase, or overwrite data. For element-targeted click and scroll, perform_action, and set_value calls, pass observation_id from the get_app_state result that supplied the element_index, object_ref, or semantic selector; stale or target-mismatched observations are rejected. type_text and press_key accept optional window_id, pid, app_id, wm_class, title, tty, terminal_pid, terminal_command, or terminal_cwd selectors and refuse targeted input if focus cannot be verified. Use run_action_batch for short, ordered click/type_text/press_key sequences against one exact window_id; use run_action_batch_and_observe when post-action state is needed so the batch and adaptive observation share one model round trip. Batches are fully prevalidated, stop at the first failure, and allow at most one leading click because clicks can invalidate later coordinates or element indices. After targeted keyboard input, results append focused-element feedback from AT-SPI (role, name, editable) and warn when no editable element holds focus — treat that warning as the input not landing. Screenshot results warn when a target window is partially or fully off-screen, and coordinate input outside the captured desktop bounds is rejected; use move_window/resize_window (GNOME Shell extension backend) to bring a window fully on-screen before retrying. Coordinate scrolls accept the same window targeting and relative coordinates as click; element-targeted scrolls require observation_id and use verified absolute element bounds. get_app_state returns a compact readiness block by default; pass verbose=true for the full diagnostics dump. Electron apps expose no AT-SPI tree unless launched with --force-renderer-accessibility."
)]
impl ServerHandler for ComputerUseLinux {}

pub async fn serve_mcp() -> Result<()> {
    ComputerUseLinux::default()
        .serve(rmcp::transport::stdio())
        .await?
        .waiting()
        .await?;
    Ok(())
}

#[derive(Debug, Clone, Serialize, JsonSchema)]
struct ListAppsOutput {
    apps: Vec<AppCandidate>,
    accessible_apps: Vec<AccessibleAppSummary>,
    accessibility_error: Option<String>,
    note: String,
}

#[derive(Debug, Clone, Serialize, JsonSchema)]
struct ListWindowsOutput {
    backend: String,
    windows: Vec<WindowInfo>,
    error: Option<String>,
    permissions_hint: Option<String>,
    note: String,
}

#[derive(Debug, Clone, Serialize, JsonSchema)]
struct FocusedWindowOutput {
    backend: String,
    focused_window: Option<WindowInfo>,
    error: Option<String>,
    permissions_hint: Option<String>,
    message: String,
}

#[derive(Debug, Clone, Default, Deserialize, Serialize, JsonSchema)]
struct ActivateWindowParams {
    #[serde(flatten)]
    claim: ClaimContext,
    #[serde(default)]
    window_id: Option<u64>,
    #[serde(default)]
    pid: Option<u32>,
    #[serde(default)]
    tty: Option<String>,
    #[serde(default)]
    terminal_pid: Option<u32>,
    #[serde(default)]
    terminal_command: Option<String>,
    #[serde(default)]
    terminal_cwd: Option<String>,
    #[serde(default)]
    app_id: Option<String>,
    #[serde(default)]
    wm_class: Option<String>,
    #[serde(default)]
    title: Option<String>,
}

impl ActivateWindowParams {
    fn into_target(self) -> WindowTarget {
        WindowTarget {
            window_id: self.window_id,
            pid: self.pid,
            tty: self.tty,
            terminal_pid: self.terminal_pid,
            terminal_command: self.terminal_command,
            terminal_cwd: self.terminal_cwd,
            app_id: self.app_id,
            wm_class: self.wm_class,
            title: self.title,
        }
    }
}

#[derive(Debug, Clone, Serialize, JsonSchema)]
struct ActivateWindowOutput {
    ok: bool,
    implemented: bool,
    backend: String,
    focus: Option<WindowFocusResult>,
    error: Option<String>,
    permissions_hint: Option<String>,
    // Echo of the request for debugging. `serde_json::Value` has no fixed JSON
    // schema, which strict MCP clients (Claude Code) reject in `outputSchema` —
    // and one invalid tool fails the whole tool list. Keep it in the runtime
    // response (serde) but omit it from the generated schema.
    #[schemars(skip)]
    received: Option<serde_json::Value>,
}

#[derive(Debug, Clone, Deserialize, Serialize, JsonSchema)]
struct MoveWindowParams {
    #[serde(flatten)]
    target: ActivateWindowParams,
    /// New frame-left in desktop coordinates.
    x: i32,
    /// New frame-top in desktop coordinates.
    y: i32,
}

#[derive(Debug, Clone, Deserialize, Serialize, JsonSchema)]
struct ResizeWindowParams {
    #[serde(flatten)]
    target: ActivateWindowParams,
    /// New frame width in desktop pixels.
    width: i32,
    /// New frame height in desktop pixels.
    height: i32,
}

#[derive(Debug, Clone, Serialize, JsonSchema)]
struct WindowGeometryOutput {
    ok: bool,
    implemented: bool,
    backend: String,
    /// Post-operation window info (compositor-final geometry).
    window: Option<WindowInfo>,
    message: String,
    permissions_hint: Option<String>,
    #[schemars(skip)]
    received: Option<serde_json::Value>,
}

#[derive(Debug, Clone, Serialize, JsonSchema)]
struct AppCandidate {
    name: String,
    pid: u32,
    command: String,
}

#[derive(Debug, Clone, Deserialize, Serialize, JsonSchema)]
struct GetAppStateParams {
    #[serde(flatten)]
    claim: ClaimContext,
    #[serde(default)]
    app_name_or_bundle_identifier: Option<String>,
    #[serde(default)]
    window_id: Option<u64>,
    #[serde(default)]
    pid: Option<u32>,
    #[serde(default)]
    tty: Option<String>,
    #[serde(default)]
    terminal_pid: Option<u32>,
    #[serde(default)]
    terminal_command: Option<String>,
    #[serde(default)]
    terminal_cwd: Option<String>,
    #[serde(default)]
    app_id: Option<String>,
    #[serde(default)]
    wm_class: Option<String>,
    #[serde(default)]
    title: Option<String>,
    #[serde(default)]
    max_nodes: Option<usize>,
    #[serde(default)]
    max_depth: Option<u32>,
    #[serde(default)]
    include_screenshot: Option<bool>,
    /// Additive observation policy. Omit to preserve the legacy full response.
    #[serde(default)]
    observation_mode: Option<ObservationMode>,
    /// Opaque checkpoint ID from the caller's last adaptive result. Omit or mismatch to force a full checkpoint.
    #[serde(default)]
    base_checkpoint_id: Option<String>,
    /// Adaptive full-checkpoint interval, clamped to 1..=32 (default 8).
    #[serde(default)]
    checkpoint_interval: Option<u32>,
    /// Force the next adaptive result to be a full checkpoint.
    #[serde(default)]
    force_checkpoint: Option<bool>,
    /// Maximum returned screenshot width in pixels (default 1920, hard-capped).
    #[serde(default)]
    max_width: Option<u32>,
    /// Maximum returned screenshot height in pixels (default 1920, hard-capped).
    #[serde(default)]
    max_height: Option<u32>,
    /// Maximum returned screenshot image bytes before base64 (default 2 MiB, hard-capped).
    #[serde(default)]
    max_bytes: Option<usize>,
    /// Additional downscale factor from 0.0 to 1.0, applied before max dimensions.
    #[serde(default)]
    scale: Option<f32>,
    /// Output image format (default png). Use jpeg with quality to trade exact pixels for smaller payloads.
    #[serde(default)]
    format: Option<ScreenshotOutputFormat>,
    /// JPEG quality from 1 to 95 (default 80). Ignored for png.
    #[serde(default)]
    #[schemars(range(min = 1, max = 95))]
    quality: Option<u8>,
    /// Include the full diagnostics report (large). Default false: only the
    /// compact readiness block is returned.
    #[serde(default)]
    verbose: Option<bool>,
}

impl GetAppStateParams {
    fn window_target(&self) -> WindowTarget {
        WindowTarget {
            window_id: self.window_id,
            pid: self.pid,
            tty: self.tty.clone(),
            terminal_pid: self.terminal_pid,
            terminal_command: self.terminal_command.clone(),
            terminal_cwd: self.terminal_cwd.clone(),
            app_id: self.app_id.clone(),
            wm_class: self.wm_class.clone(),
            title: self.title.clone(),
        }
    }

    fn screenshot_options(&self) -> ScreenshotPayloadOptions {
        ScreenshotPayloadOptions {
            max_width: self.max_width,
            max_height: self.max_height,
            max_bytes: self.max_bytes,
            scale: self.scale,
            format: self.format,
            quality: self.quality,
        }
    }
}

#[derive(Debug, Default, Deserialize, JsonSchema)]
struct PostActionObservationParams {
    /// Opaque checkpoint ID from the caller's last adaptive observation.
    #[serde(default)]
    base_checkpoint_id: Option<String>,
    /// Adaptive full-checkpoint interval, clamped to 1..=32 (default 8).
    #[serde(default)]
    checkpoint_interval: Option<u32>,
    /// Force the post-action observation to be a full checkpoint.
    #[serde(default)]
    force_checkpoint: Option<bool>,
    #[serde(default)]
    include_screenshot: Option<bool>,
    #[serde(default)]
    max_width: Option<u32>,
    #[serde(default)]
    max_height: Option<u32>,
    #[serde(default)]
    max_bytes: Option<usize>,
    #[serde(default)]
    scale: Option<f32>,
    #[serde(default)]
    format: Option<ScreenshotOutputFormat>,
    #[serde(default)]
    #[schemars(range(min = 1, max = 95))]
    quality: Option<u8>,
    #[serde(default)]
    max_nodes: Option<usize>,
    #[serde(default)]
    max_depth: Option<u32>,
}

impl PostActionObservationParams {
    fn into_get_app_state_params(self, window_id: u64, claim: ClaimContext) -> GetAppStateParams {
        GetAppStateParams {
            claim,
            app_name_or_bundle_identifier: None,
            window_id: Some(window_id),
            pid: None,
            tty: None,
            terminal_pid: None,
            terminal_command: None,
            terminal_cwd: None,
            app_id: None,
            wm_class: None,
            title: None,
            max_nodes: self.max_nodes,
            max_depth: self.max_depth,
            include_screenshot: self.include_screenshot,
            observation_mode: Some(ObservationMode::Adaptive),
            base_checkpoint_id: self.base_checkpoint_id,
            checkpoint_interval: self.checkpoint_interval,
            force_checkpoint: self.force_checkpoint,
            max_width: self.max_width,
            max_height: self.max_height,
            max_bytes: self.max_bytes,
            scale: self.scale,
            format: self.format,
            quality: self.quality,
            verbose: Some(false),
        }
    }
}

#[derive(Debug, Deserialize, JsonSchema)]
struct ActionBatchAndObserveParams {
    #[serde(flatten)]
    batch: ActionBatchParams,
    #[serde(default)]
    observation: PostActionObservationParams,
}

#[derive(Debug, Serialize, JsonSchema)]
struct ActionBatchAndObserveOutput {
    batch: ActionBatchOutput,
    #[schemars(with = "Option<GetAppStateOutput>")]
    observation: Option<serde_json::Value>,
    observation_error: Option<String>,
}

#[derive(Debug, Clone, Default, Deserialize, Serialize, JsonSchema)]
struct ScreenshotParams {
    #[serde(flatten)]
    claim: ClaimContext,
    #[serde(default)]
    window_id: Option<u64>,
    #[serde(default)]
    pid: Option<u32>,
    #[serde(default)]
    app_id: Option<String>,
    #[serde(default)]
    wm_class: Option<String>,
    #[serde(default)]
    title: Option<String>,
    /// Raise the targeted window before capture (default true). Ignored without
    /// a window target.
    #[serde(default)]
    raise_window: Option<bool>,
    /// Capture the whole desktop even when a window is targeted (default false).
    #[serde(default)]
    full_screen: Option<bool>,
    /// Maximum returned screenshot width in pixels (default 1920, hard-capped).
    #[serde(default)]
    max_width: Option<u32>,
    /// Maximum returned screenshot height in pixels (default 1920, hard-capped).
    #[serde(default)]
    max_height: Option<u32>,
    /// Maximum returned screenshot image bytes before base64 (default 2 MiB, hard-capped).
    #[serde(default)]
    max_bytes: Option<usize>,
    /// Additional downscale factor from 0.0 to 1.0, applied before max dimensions.
    #[serde(default)]
    scale: Option<f32>,
    /// Output image format (default png). Use jpeg with quality to trade exact pixels for smaller payloads.
    #[serde(default)]
    format: Option<ScreenshotOutputFormat>,
    /// JPEG quality from 1 to 95 (default 80). Ignored for png.
    #[serde(default)]
    #[schemars(range(min = 1, max = 95))]
    quality: Option<u8>,
}

impl ScreenshotParams {
    fn window_target(&self) -> Option<WindowTarget> {
        if self.window_id.is_none()
            && self.pid.is_none()
            && self.app_id.is_none()
            && self.wm_class.is_none()
            && self.title.is_none()
        {
            return None;
        }
        Some(WindowTarget {
            window_id: self.window_id,
            pid: self.pid,
            tty: None,
            terminal_pid: None,
            terminal_command: None,
            terminal_cwd: None,
            app_id: self.app_id.clone(),
            wm_class: self.wm_class.clone(),
            title: self.title.clone(),
        })
    }

    fn screenshot_options(&self) -> ScreenshotPayloadOptions {
        ScreenshotPayloadOptions {
            max_width: self.max_width,
            max_height: self.max_height,
            max_bytes: self.max_bytes,
            scale: self.scale,
            format: self.format,
            quality: self.quality,
        }
    }
}

#[derive(Debug, Clone, Serialize, JsonSchema)]
struct GetAppStateOutput {
    app_name_or_bundle_identifier: Option<String>,
    window_context: Option<WindowInfo>,
    window_error: Option<String>,
    window_permissions_hint: Option<String>,
    backend: String,
    screenshot: Option<ScreenshotMetadata>,
    screenshot_regions: Vec<ScreenshotRegionMetadata>,
    screenshot_error: Option<String>,
    accessibility_tree: Vec<AccessibilityNode>,
    accessibility_tree_raw_count: usize,
    /// Opaque ID for this bounded accessibility snapshot.
    observation_id: Option<String>,
    accessibility_coordinate_space: String,
    accessibility_error: Option<String>,
    /// Compact readiness summary (always present).
    readiness: crate::diagnostics::ReadinessReport,
    /// Full diagnostics; populated only when verbose=true.
    #[serde(skip_serializing_if = "Option::is_none")]
    diagnostics: Option<DoctorReport>,
    /// Present only for observation_mode=adaptive.
    #[serde(skip_serializing_if = "Option::is_none")]
    observation: Option<AdaptiveObservationMetadata>,
    message: String,
}

#[derive(Debug, Clone, Serialize, JsonSchema)]
struct ScreenshotRegionMetadata {
    /// Region coordinates in the current observation frame.
    region: ObservationRegion,
    screenshot: ScreenshotMetadata,
}

impl ScreenshotRegionMetadata {
    fn from_capture(
        region: ObservationRegion,
        capture: &ScreenshotCapture,
        window: Option<&WindowInfo>,
        frame_origin: (u32, u32),
    ) -> Self {
        let coordinate_origin = (
            frame_origin.0.saturating_add(region.x),
            frame_origin.1.saturating_add(region.y),
        );
        Self {
            region,
            screenshot: ScreenshotMetadata::from_capture(capture, window, coordinate_origin),
        }
    }
}

#[derive(Debug, Clone, Serialize, JsonSchema)]
struct ScreenshotMetadata {
    mime_type: String,
    source: String,
    width: u32,
    height: u32,
    coordinate_width: u32,
    coordinate_height: u32,
    scale: f32,
    resized: bool,
    bytes: usize,
    original_bytes: usize,
    max_bytes: usize,
    format: ScreenshotOutputFormat,
    quality: Option<u8>,
    coordinate_space: String,
    coordinate_origin_x: u32,
    coordinate_origin_y: u32,
    window_id: Option<u64>,
}

impl ScreenshotMetadata {
    fn from_capture(
        capture: &ScreenshotCapture,
        window: Option<&WindowInfo>,
        coordinate_origin: (u32, u32),
    ) -> Self {
        Self {
            mime_type: capture.mime_type.clone(),
            source: capture.source.clone(),
            width: capture.width,
            height: capture.height,
            coordinate_width: capture.coordinate_width,
            coordinate_height: capture.coordinate_height,
            scale: capture.scale,
            resized: capture.resized,
            bytes: capture.bytes,
            original_bytes: capture.original_bytes,
            max_bytes: capture.max_bytes,
            format: capture.format,
            quality: capture.quality,
            coordinate_space: if window.is_some() {
                "window_local".to_string()
            } else {
                "desktop".to_string()
            },
            coordinate_origin_x: coordinate_origin.0,
            coordinate_origin_y: coordinate_origin.1,
            window_id: window.map(|window| window.window_id),
        }
    }
}

#[derive(Debug, Clone, Default, Deserialize, Serialize, JsonSchema)]
struct ClickParams {
    #[serde(flatten)]
    claim: ClaimContext,
    /// Opaque ID returned by get_app_state. Required for element-based clicks.
    #[serde(default)]
    observation_id: Option<String>,
    #[serde(default)]
    element_index: Option<u32>,
    #[serde(default)]
    role: Option<String>,
    #[serde(default)]
    name: Option<String>,
    #[serde(default)]
    text: Option<String>,
    #[serde(default)]
    states: Vec<String>,
    #[serde(default)]
    x: Option<i32>,
    #[serde(default)]
    y: Option<i32>,
    #[serde(default)]
    button: Option<String>,
    #[serde(default)]
    click_count: Option<u32>,
    // Optional window target: every click route verifies it before acting;
    // coordinate clicks additionally wait for the window stack to settle.
    #[serde(default)]
    window_id: Option<u64>,
    #[serde(default)]
    pid: Option<u32>,
    #[serde(default)]
    app_id: Option<String>,
    #[serde(default)]
    wm_class: Option<String>,
    #[serde(default)]
    window_title: Option<String>,
    /// Interpret `x`/`y` as relative to the targeted window's top-left corner
    /// (the same coordinate space as a window-cropped `screenshot`). Requires a
    /// window target; ignored otherwise.
    #[serde(default)]
    relative: Option<bool>,
}

impl ClickParams {
    /// A window target if any window-identifying field was supplied.
    fn window_target(&self) -> Option<WindowTarget> {
        if self.window_id.is_none()
            && self.pid.is_none()
            && self.app_id.is_none()
            && self.wm_class.is_none()
            && self.window_title.is_none()
        {
            return None;
        }
        Some(WindowTarget {
            window_id: self.window_id,
            pid: self.pid,
            tty: None,
            terminal_pid: None,
            terminal_command: None,
            terminal_cwd: None,
            app_id: self.app_id.clone(),
            wm_class: self.wm_class.clone(),
            title: self.window_title.clone(),
        })
    }

    fn selector(&self) -> ElementSelector<'_> {
        ElementSelector {
            role: self.role.as_deref(),
            name: self.name.as_deref(),
            text: self.text.as_deref(),
            states: &self.states,
        }
    }
}

impl BatchClick {
    fn into_click_params(self, window_id: u64, claim: ClaimContext) -> ClickParams {
        ClickParams {
            claim,
            observation_id: self.observation_id,
            element_index: self.element_index,
            role: self.role,
            name: self.name,
            text: self.text,
            states: self.states,
            x: self.x,
            y: self.y,
            button: self.button,
            click_count: self.click_count,
            window_id: Some(window_id),
            pid: None,
            app_id: None,
            wm_class: None,
            window_title: None,
            relative: self.relative,
        }
    }
}

#[derive(Debug, Clone, Default, Deserialize, Serialize, JsonSchema)]
struct ActionParams {
    #[serde(flatten)]
    claim: ClaimContext,
    /// Opaque ID returned by get_app_state for the selected element.
    observation_id: String,
    #[serde(default)]
    element_index: Option<u32>,
    #[serde(default)]
    element_identifier: Option<String>,
    #[serde(default)]
    role: Option<String>,
    #[serde(default)]
    name: Option<String>,
    #[serde(default)]
    text: Option<String>,
    #[serde(default)]
    states: Vec<String>,
    #[serde(default)]
    action: Option<String>,
}

impl ActionParams {
    fn selector(&self) -> ElementSelector<'_> {
        ElementSelector {
            role: self.role.as_deref(),
            name: self.name.as_deref(),
            text: self.text.as_deref(),
            states: &self.states,
        }
    }
}

#[derive(Debug, Clone, Default, Deserialize, Serialize, JsonSchema)]
struct SetValueParams {
    #[serde(flatten)]
    claim: ClaimContext,
    /// Opaque ID returned by get_app_state for the selected element.
    observation_id: String,
    #[serde(default)]
    element_index: Option<u32>,
    #[serde(default)]
    element_identifier: Option<String>,
    #[serde(default)]
    role: Option<String>,
    #[serde(default)]
    name: Option<String>,
    #[serde(default)]
    text: Option<String>,
    #[serde(default)]
    states: Vec<String>,
    value: String,
}

impl SetValueParams {
    fn selector(&self) -> ElementSelector<'_> {
        ElementSelector {
            role: self.role.as_deref(),
            name: self.name.as_deref(),
            text: self.text.as_deref(),
            states: &self.states,
        }
    }
}

#[derive(Debug, Clone, Default, Deserialize, Serialize, JsonSchema)]
struct ScrollParams {
    #[serde(flatten)]
    claim: ClaimContext,
    /// Required with element_index; returned by the originating get_app_state.
    #[serde(default)]
    observation_id: Option<String>,
    #[serde(default)]
    element_index: Option<u32>,
    #[serde(default)]
    x: Option<i32>,
    #[serde(default)]
    y: Option<i32>,
    direction: String,
    #[serde(default)]
    pages: Option<f64>,
    // Optional window target (parity with click): the window is raised/focused
    // before scrolling so the wheel events land on the intended app.
    #[serde(default)]
    window_id: Option<u64>,
    #[serde(default)]
    pid: Option<u32>,
    #[serde(default)]
    app_id: Option<String>,
    #[serde(default)]
    wm_class: Option<String>,
    #[serde(default)]
    window_title: Option<String>,
    /// Interpret `x`/`y` as relative to the targeted window's top-left corner
    /// (the same coordinate space as a window-cropped `screenshot`). Requires a
    /// window target; ignored otherwise.
    #[serde(default)]
    relative: Option<bool>,
}

impl ScrollParams {
    /// A window target if any window-identifying field was supplied.
    fn window_target(&self) -> Option<WindowTarget> {
        if self.window_id.is_none()
            && self.pid.is_none()
            && self.app_id.is_none()
            && self.wm_class.is_none()
            && self.window_title.is_none()
        {
            return None;
        }
        Some(WindowTarget {
            window_id: self.window_id,
            pid: self.pid,
            tty: None,
            terminal_pid: None,
            terminal_command: None,
            terminal_cwd: None,
            app_id: self.app_id.clone(),
            wm_class: self.wm_class.clone(),
            title: self.window_title.clone(),
        })
    }
}

#[derive(Debug, Clone, Default, Deserialize, Serialize, JsonSchema)]
struct DragParams {
    #[serde(flatten)]
    claim: ClaimContext,
    start_x: i32,
    start_y: i32,
    end_x: i32,
    end_y: i32,
    #[serde(default)]
    window_id: Option<u64>,
    #[serde(default)]
    pid: Option<u32>,
    #[serde(default)]
    app_id: Option<String>,
    #[serde(default)]
    wm_class: Option<String>,
    #[serde(default)]
    window_title: Option<String>,
}

impl DragParams {
    fn window_target(&self) -> WindowTarget {
        WindowTarget {
            window_id: self.window_id,
            pid: self.pid,
            app_id: self.app_id.clone(),
            wm_class: self.wm_class.clone(),
            title: self.window_title.clone(),
            ..Default::default()
        }
    }
}

#[derive(Debug, Clone, Deserialize, Serialize, JsonSchema)]
struct PressKeyParams {
    #[serde(flatten)]
    claim: ClaimContext,
    key: String,
    #[serde(default)]
    window_id: Option<u64>,
    #[serde(default)]
    pid: Option<u32>,
    #[serde(default)]
    tty: Option<String>,
    #[serde(default)]
    terminal_pid: Option<u32>,
    #[serde(default)]
    terminal_command: Option<String>,
    #[serde(default)]
    terminal_cwd: Option<String>,
    #[serde(default)]
    app_id: Option<String>,
    #[serde(default)]
    wm_class: Option<String>,
    #[serde(default)]
    title: Option<String>,
}

#[derive(Debug, Clone, Deserialize, Serialize, JsonSchema)]
struct TypeTextParams {
    #[serde(flatten)]
    claim: ClaimContext,
    text: String,
    #[serde(default)]
    window_id: Option<u64>,
    #[serde(default)]
    pid: Option<u32>,
    #[serde(default)]
    tty: Option<String>,
    #[serde(default)]
    terminal_pid: Option<u32>,
    #[serde(default)]
    terminal_command: Option<String>,
    #[serde(default)]
    terminal_cwd: Option<String>,
    #[serde(default)]
    app_id: Option<String>,
    #[serde(default)]
    wm_class: Option<String>,
    #[serde(default)]
    title: Option<String>,
}

impl PressKeyParams {
    fn for_window(window_id: u64, key: String, claim: ClaimContext) -> Self {
        Self {
            claim,
            key,
            window_id: Some(window_id),
            pid: None,
            tty: None,
            terminal_pid: None,
            terminal_command: None,
            terminal_cwd: None,
            app_id: None,
            wm_class: None,
            title: None,
        }
    }

    fn window_target(&self) -> WindowTarget {
        WindowTarget {
            window_id: self.window_id,
            pid: self.pid,
            tty: self.tty.clone(),
            terminal_pid: self.terminal_pid,
            terminal_command: self.terminal_command.clone(),
            terminal_cwd: self.terminal_cwd.clone(),
            app_id: self.app_id.clone(),
            wm_class: self.wm_class.clone(),
            title: self.title.clone(),
        }
    }
}

impl TypeTextParams {
    fn for_window(window_id: u64, text: String, claim: ClaimContext) -> Self {
        Self {
            claim,
            text,
            window_id: Some(window_id),
            pid: None,
            tty: None,
            terminal_pid: None,
            terminal_command: None,
            terminal_cwd: None,
            app_id: None,
            wm_class: None,
            title: None,
        }
    }

    fn window_target(&self) -> WindowTarget {
        WindowTarget {
            window_id: self.window_id,
            pid: self.pid,
            tty: self.tty.clone(),
            terminal_pid: self.terminal_pid,
            terminal_command: self.terminal_command.clone(),
            terminal_cwd: self.terminal_cwd.clone(),
            app_id: self.app_id.clone(),
            wm_class: self.wm_class.clone(),
            title: self.title.clone(),
        }
    }
}

impl ComputerUseLinux {
    async fn execute_validated_action_batch_unlocked(
        &self,
        params: ActionBatchParams,
    ) -> ActionBatchOutput {
        let claim = params.claim.clone();
        execute_action_batch(params, |action, window_id| {
            let claim = claim.clone();
            async move {
                match action {
                    BatchAction::Click(click) => {
                        let Json(result) = self
                            .click_unlocked(
                                click.into_click_params(window_id, claim.clone()),
                                ClaimGuardMode::AlreadyHeld,
                            )
                            .await;
                        BatchActionRun::Completed(result)
                    }
                    BatchAction::TypeText { text } => {
                        let Json(result) = self
                            .type_text_unlocked(
                                TypeTextParams::for_window(window_id, text, claim.clone()),
                                ClaimGuardMode::AlreadyHeld,
                            )
                            .await;
                        BatchActionRun::text(result)
                    }
                    BatchAction::PressKey { key } => {
                        let Json(result) = self
                            .press_key_unlocked(
                                PressKeyParams::for_window(window_id, key, claim.clone()),
                                ClaimGuardMode::AlreadyHeld,
                            )
                            .await;
                        BatchActionRun::Completed(result)
                    }
                }
            }
        })
        .await
    }

    fn validate_action_batch(&self, params: &ActionBatchParams) -> Result<(), String> {
        params.validate()?;
        for (index, action) in params.actions.iter().enumerate() {
            match action {
                BatchAction::Click(click) => {
                    let click = click
                        .clone()
                        .into_click_params(params.window_id, params.claim.clone());
                    self.resolve_observed_click_target(&click)
                        .map_err(|error| format!("actions[{index}] is invalid: {error}"))?;
                }
                BatchAction::PressKey { key } if key_sequence(key).is_none() => {
                    return Err(format!(
                        "actions[{index}] has an unsupported key. Use names like Enter, Escape, Tab, ArrowLeft, Ctrl+L, or a single US keyboard letter/digit."
                    ));
                }
                BatchAction::TypeText { .. } | BatchAction::PressKey { .. } => {}
            }
        }
        Ok(())
    }

    fn is_wayland_session(&self) -> bool {
        crate::diagnostics::hydrate_session_bus_env();
        let session_type = env::var("XDG_SESSION_TYPE").ok();
        let wayland_display = env::var("WAYLAND_DISPLAY").ok();
        session_is_wayland(session_type.as_deref(), wayland_display.as_deref())
    }

    // The Wayland remote-desktop portal is now a *fallback* for input: when a
    // compatible ydotool CLI and working `ydotoold` socket are present we prefer
    // ydotool, because it injects input without a permission prompt. GNOME
    // refuses to persist remote-desktop
    // grants (`org.freedesktop.portal.Error: Remote desktop sessions cannot
    // persist`), so the portal would otherwise re-prompt on every new session.
    // A ydotool force overrides a portal force; absolute uinput still remains
    // preferred for click/drag unless explicitly disabled.
    fn should_prefer_portal_pointer_backend(&self) -> bool {
        let overrides = PointerInputOverrides::from_env();
        if overrides.ydotool_pointer_forced {
            return false;
        }
        if overrides.portal_pointer_forced {
            return self.is_wayland_session();
        }
        should_prefer_portal_backend_by_default(
            self.is_wayland_session(),
            ydotool_backend_available(),
        )
    }

    fn can_fallback_to_ydotool_for_coordinate_action(&self) -> bool {
        PointerInputOverrides::from_env().allows_ydotool() && ydotool_backend_available()
    }

    fn should_prefer_portal_keyboard_backend(&self) -> bool {
        if env_flag_enabled("COMPUTER_USE_LINUX_FORCE_YDOTOOL_KEYBOARD") {
            return false;
        }
        if env_flag_enabled("COMPUTER_USE_LINUX_FORCE_PORTAL_KEYBOARD") {
            return self.is_wayland_session() && !self.is_kde_wayland_session();
        }
        !self.is_kde_wayland_session()
            && should_prefer_portal_backend_by_default(
                self.is_wayland_session(),
                ydotool_backend_available(),
            )
    }

    fn should_prefer_kde_clipboard_text_backend(&self) -> bool {
        !env_flag_enabled("COMPUTER_USE_LINUX_FORCE_YDOTOOL_KEYBOARD")
            && self.is_kde_wayland_session()
    }

    fn is_kde_wayland_session(&self) -> bool {
        self.is_wayland_session()
            && (env_contains("XDG_CURRENT_DESKTOP", "kde")
                || env_contains("DESKTOP_SESSION", "plasma"))
    }

    fn cached_portal_pointer_session(&self) -> Option<PortalPointerSession> {
        self.portal_session
            .lock()
            .ok()
            .and_then(|cached| cached.clone())
            .filter(PortalSession::has_pointer)
    }

    async fn portal_pointer_session_for_action(&self) -> Option<PortalPointerSession> {
        if let Some(session) = self.cached_portal_pointer_session() {
            return Some(session);
        }
        if !self.should_prefer_portal_pointer_backend() {
            return None;
        }
        self.ensure_portal_pointer_session().await.ok().flatten()
    }

    async fn run_portal_pointer_action<F>(
        &self,
        action: F,
    ) -> std::result::Result<(), PortalActionError>
    where
        F: Future<Output = std::result::Result<(), PortalActionError>> + Send + 'static,
    {
        let cached_session = Arc::clone(&self.portal_session);
        let task = tokio::spawn(async move {
            let result = action.await;
            if result.is_err() {
                if let Ok(mut cached) = cached_session.lock() {
                    *cached = None;
                }
            }
            result
        });
        match task.await {
            Ok(result) => result,
            Err(error) => {
                self.clear_portal_pointer_session();
                Err(PortalActionError::MayHaveDelivered(anyhow::anyhow!(
                    "portal pointer action task failed: {error}"
                )))
            }
        }
    }

    fn clear_portal_pointer_session(&self) {
        if let Ok(mut cached) = self.portal_session.lock() {
            *cached = None;
        }
    }

    fn cached_portal_keyboard_session(&self) -> Option<PortalKeyboardSession> {
        self.portal_session
            .lock()
            .ok()
            .and_then(|cached| cached.clone())
            .filter(PortalSession::has_keyboard)
    }

    fn clear_portal_keyboard_session(&self) {
        if let Ok(mut cached) = self.portal_session.lock() {
            *cached = None;
        }
    }

    async fn ensure_portal_session(&self) -> Result<PortalSession> {
        if let Some(session) = self
            .portal_session
            .lock()
            .ok()
            .and_then(|cached| cached.clone())
        {
            return Ok(session);
        }

        let _guard = self.portal_session_init_lock.lock().await;
        if let Some(session) = self
            .portal_session
            .lock()
            .ok()
            .and_then(|cached| cached.clone())
        {
            return Ok(session);
        }

        let session = start_portal_session().await?;
        if let Ok(mut cached) = self.portal_session.lock() {
            *cached = Some(session.clone());
        }
        Ok(session)
    }

    async fn ensure_portal_pointer_session(&self) -> Result<Option<PortalPointerSession>> {
        if !self.should_prefer_portal_pointer_backend() {
            return Ok(None);
        }
        if let Some(session) = self.cached_portal_pointer_session() {
            return Ok(Some(session));
        }

        let session = self.ensure_portal_session().await?;
        Ok(session.has_pointer().then_some(session))
    }

    async fn ensure_portal_keyboard_session(&self) -> Result<Option<PortalKeyboardSession>> {
        if env_flag_enabled("COMPUTER_USE_LINUX_FORCE_YDOTOOL_KEYBOARD")
            || !self.is_wayland_session()
        {
            return Ok(None);
        }
        if let Some(session) = self.cached_portal_keyboard_session() {
            return Ok(Some(session));
        }

        let session = self.ensure_portal_session().await?;
        Ok(session.has_keyboard().then_some(session))
    }

    async fn resolve_window_context(
        &self,
        params: &GetAppStateParams,
    ) -> (Option<WindowInfo>, Option<String>, Option<String>) {
        let target = params.window_target();
        if !target.has_target() {
            return (None, None, None);
        }

        match list_windows().await {
            Ok(windows) => match resolve_window_target(&windows, &target) {
                Ok(window) => (Some(window.clone()), None, None),
                Err(error) => (None, Some(format!("{error:#}")), None),
            },
            Err(error) => {
                let error = format!("{error:#}");
                let hint = window_permission_hint(&error);
                (None, Some(error), hint)
            }
        }
    }

    async fn resolve_accessibility_app_filter(
        &self,
        params: &GetAppStateParams,
        window_context: Option<&WindowInfo>,
    ) -> Option<String> {
        if let Some(explicit) = trimmed_nonempty(params.app_name_or_bundle_identifier.as_deref()) {
            return Some(explicit.to_string());
        }

        let target_pid = window_context.and_then(|window| window.pid).or(params.pid);
        let candidates = accessibility_filter_candidates(window_context);

        if let Some(target_pid) = target_pid {
            if let Ok(apps) = list_accessible_apps(200).await {
                if let Some(object_ref) =
                    select_accessibility_object_ref(&apps, target_pid, &candidates)
                {
                    return Some(object_ref);
                }
            }
        }

        candidates.into_iter().next()
    }

    async fn focus_target_for_input(
        &self,
        target: &WindowTarget,
    ) -> std::result::Result<Option<WindowFocusResult>, String> {
        if !target.has_target() {
            return Ok(None);
        }

        let focus = focus_window_target(target).await.map_err(|error| {
            let error = format!("{error:#}");
            if let Some(hint) = window_permission_hint(&error) {
                format!("Did not send input because the target window could not be focused: {error}. {hint}")
            } else {
                format!("Did not send input because the target window could not be focused: {error}")
            }
        })?;

        if focus_satisfies_target(&focus, target) {
            Ok(Some(focus))
        } else {
            let required = if target.requires_exact_focus() {
                "exact target-window focus"
            } else {
                "app-level focus"
            };
            Err(format!(
                "Did not send input because {required} verification failed after activating the target window. Focus result: requested window_id {}, focused window_id {:?}.",
                focus.requested_window.window_id,
                focus.focused_window.as_ref().map(|window| window.window_id)
            ))
        }
    }

    fn cache_desktop_size(&self, width: u32, height: u32) {
        if width == 0 || height == 0 {
            return;
        }
        if let Ok(mut guard) = self.desktop_size.lock() {
            *guard = Some((width, height));
        }
    }

    /// COORDINATE SPACES: window bounds (list_windows / extension frame rects)
    /// and the extension monitor layout are in LOGICAL pixels, while click/
    /// scroll coordinates and screenshot captures are in PHYSICAL capture
    /// pixels. On fractionally-scaled displays the two differ, so each check
    /// below only ever compares values from the same space.
    ///
    /// Logical monitor rectangles from the GNOME Shell extension, for checks
    /// against logical window bounds. None when the extension is unavailable.
    async fn logical_monitor_rects(&self) -> Option<Vec<(i32, i32, i32, i32)>> {
        let monitors = crate::windowing::backends::gnome::extension_monitor_layout()
            .await
            .ok()?;
        (!monitors.is_empty()).then(|| {
            monitors
                .iter()
                .map(|m| (m.x, m.y, m.width, m.height))
                .collect()
        })
    }

    /// Physical capture-space desktop rectangle (union of monitors as captured
    /// by the screenshot pipeline), for checks against click coordinates.
    /// Best-effort; None disables the check.
    async fn capture_space_rect(&self) -> Option<(i32, i32, i32, i32)> {
        let cached = self.desktop_size.lock().ok().and_then(|guard| *guard);
        if let Some((w, h)) = cached {
            return Some((0, 0, w as i32, h as i32));
        }
        // One-time prime: a full-frame capture reveals the desktop size when
        // no prior capture is available.
        let raw = capture_screenshot_raw().await.ok()?;
        self.cache_desktop_size(raw.width, raw.height);
        (raw.width > 0 && raw.height > 0).then_some((0, 0, raw.width as i32, raw.height as i32))
    }

    /// Warn when a targeted window pokes outside every monitor: clicks and
    /// screenshots silently truncate to visible pixels there, which reads as
    /// "success" while landing nowhere.
    async fn off_screen_note_for_bounds(
        &self,
        bounds: &crate::windowing::WindowBounds,
    ) -> Option<String> {
        let (x, y) = bounds.x.zip(bounds.y)?;
        if bounds.width == 0 || bounds.height == 0 {
            return None;
        }
        // Window bounds are logical pixels: prefer the extension's logical
        // monitor layout (same space). The physical capture rect is a safe
        // fallback — on scaled displays it is at least as large as the logical
        // union, so it can only under-warn, never false-positive.
        let rects = match self.logical_monitor_rects().await {
            Some(rects) => rects,
            None => vec![self.capture_space_rect().await?],
        };
        let (w, h) = (bounds.width as i64, bounds.height as i64);
        let window_area = w * h;
        let mut visible_area = 0_i64;
        for (mx, my, mw, mh) in &rects {
            let ix = (x as i64).max(*mx as i64);
            let iy = (y as i64).max(*my as i64);
            let ix2 = (x as i64 + w).min(*mx as i64 + *mw as i64);
            let iy2 = (y as i64 + h).min(*my as i64 + *mh as i64);
            if ix2 > ix && iy2 > iy {
                // Overlapping monitors are rare; treating them as additive keeps
                // this a cheap best-effort heuristic.
                visible_area += (ix2 - ix) * (iy2 - iy);
            }
        }
        let visible_pct = (visible_area.min(window_area) * 100) / window_area.max(1);
        if visible_pct >= 100 {
            return None;
        }
        Some(format!(
            "WARNING: the target window (bounds {x},{y} {w}x{h}) is only ~{visible_pct}% on-screen; off-screen regions are missing from screenshots and unreachable by coordinate input. Use move_window/resize_window to bring it fully on-screen."
        ))
    }

    /// Refuse coordinate input unless the point is inside a known desktop
    /// coordinate space. Screenshot coordinates use the physical capture rect,
    /// while AT-SPI and window-derived coordinates can use a logical monitor
    /// rect (including monitors placed left of or above the primary display).
    async fn validate_capture_space_point(
        &self,
        x: i32,
        y: i32,
    ) -> std::result::Result<(), String> {
        let capture_rect = self.capture_space_rect().await.ok_or_else(|| {
            "Could not establish the addressable desktop bounds; refusing coordinate input."
                .to_string()
        })?;
        let logical_rects = self.logical_monitor_rects().await.unwrap_or_default();
        if !point_in_addressable_desktop((x, y), capture_rect, &logical_rects) {
            let (_, _, width, height) = capture_rect;
            return Err(format!(
                "Coordinate {x},{y} is outside the addressable desktop ({width}x{height} capture space); no input was sent."
            ));
        }
        Ok(())
    }

    /// Post-input feedback: which AT-SPI element holds keyboard focus in the
    /// target app, and whether it is editable. Guards against the blind-typing
    /// trap where verified *window* focus still sends keystrokes nowhere.
    async fn focused_element_feedback(
        &self,
        focus: Option<&WindowFocusResult>,
        expects_editable: bool,
    ) -> Option<String> {
        let focus = focus?;
        let pid = focus
            .focused_window
            .as_ref()
            .and_then(|window| window.pid)
            .or(focus.requested_window.pid);
        match timeout(Duration::from_millis(1500), focused_element_summary(pid)).await {
            Ok(Ok(Some(element))) => Some(describe_focused_element(&element, expects_editable)),
            Ok(Ok(None)) => Some(
                format!(
                    "WARNING: AT-SPI reports no focused element in the target app — {NO_FOCUSED_ELEMENT_TEXT_LANDING_WARNING}. If this is an Electron app, launch it with --force-renderer-accessibility to expose its UI tree."
                ),
            ),
            Ok(Err(error)) => Some(format!(
                "Focused-element feedback unavailable ({}).",
                first_line(&format!("{error:#}"))
            )),
            Err(_) => Some("Focused-element feedback unavailable (AT-SPI probe timed out).".to_string()),
        }
    }

    /// Shared move/resize plumbing: resolve the window target, run the GNOME
    /// Shell extension operation, then re-query bounds to report the result.
    async fn window_geometry_op<F, Fut>(
        &self,
        received: Option<serde_json::Value>,
        target: &WindowTarget,
        op: F,
    ) -> Json<WindowGeometryOutput>
    where
        F: FnOnce(crate::windowing::WindowInfo) -> Fut,
        Fut: Future<Output = Result<String>>,
    {
        let windows = match list_windows().await {
            Ok(windows) => windows,
            Err(error) => {
                let error = format!("{error:#}");
                return Json(WindowGeometryOutput {
                    ok: false,
                    implemented: true,
                    backend: "unknown".to_string(),
                    window: None,
                    message: format!("Window listing failed: {error}"),
                    permissions_hint: window_permission_hint(&error),
                    received,
                });
            }
        };
        let window = match resolve_window_target(&windows, target) {
            Ok(window) => window.clone(),
            Err(error) => {
                return Json(WindowGeometryOutput {
                    ok: false,
                    implemented: true,
                    backend: "unknown".to_string(),
                    window: None,
                    message: format!("{error:#}"),
                    permissions_hint: None,
                    received,
                });
            }
        };
        let backend = window.backend.clone();
        let window_id = window.window_id;
        match op(window).await {
            Ok(message) => {
                // Re-query so the caller sees the compositor-final geometry
                // (tiling constraints, minimum sizes, etc. may adjust it).
                let window = list_windows().await.ok().and_then(|windows| {
                    windows
                        .into_iter()
                        .find(|window| window.window_id == window_id)
                });
                let mut message = message;
                if let Some(bounds) = window.as_ref().and_then(|window| window.bounds.as_ref()) {
                    if let Some(note) = self.off_screen_note_for_bounds(bounds).await {
                        message = format!("{message} {note}");
                    }
                }
                Json(WindowGeometryOutput {
                    ok: true,
                    implemented: true,
                    backend,
                    window,
                    message,
                    permissions_hint: None,
                    received,
                })
            }
            Err(error) => {
                let error = format!("{error:#}");
                Json(WindowGeometryOutput {
                    ok: false,
                    implemented: true,
                    backend,
                    window: None,
                    permissions_hint: window_permission_hint(&error),
                    message: error,
                    received,
                })
            }
        }
    }

    /// Notes appended after targeted keyboard input: off-screen window warning
    /// plus focused-element feedback.
    async fn input_landing_notes(
        &self,
        focus: Option<&WindowFocusResult>,
        expects_editable: bool,
    ) -> Vec<String> {
        let mut notes = Vec::new();
        if let Some(focus) = focus {
            let bounds = focus
                .focused_window
                .as_ref()
                .and_then(|window| window.bounds.as_ref())
                .or(focus.requested_window.bounds.as_ref());
            if let Some(bounds) = bounds {
                if let Some(note) = self.off_screen_note_for_bounds(bounds).await {
                    notes.push(note);
                }
            }
        }
        if let Some(note) = self.focused_element_feedback(focus, expects_editable).await {
            notes.push(note);
        }
        notes
    }

    async fn mutation_claim_guard(
        &self,
        mode: ClaimGuardMode,
        window_id: Option<u64>,
        context: &ClaimContext,
        lane: MutationLane,
    ) -> std::result::Result<Option<crate::claim_coordination::MutationGuards>, String> {
        match mode {
            ClaimGuardMode::Acquire => {
                let coordinator = self
                    .claim_coordinator
                    .get_or_init(Coordinator::from_env)
                    .clone();
                acquire_mutation_guards(coordinator, window_id, context, lane).await
            }
            ClaimGuardMode::AlreadyHeld => Ok(None),
        }
    }

    async fn coordination_window_id(
        &self,
        target: Option<&WindowTarget>,
    ) -> std::result::Result<Option<u64>, String> {
        let Some(target) = target.filter(|target| target.has_target()) else {
            return Ok(None);
        };
        if let Some(window_id) = target.window_id {
            return Ok(Some(window_id));
        }
        let windows = list_windows()
            .await
            .map_err(|error| format!("failed to resolve claim window: {error:#}"))?;
        resolve_window_target(&windows, target)
            .map(|window| Some(window.window_id))
            .map_err(|error| format!("failed to resolve claim window: {error:#}"))
    }

    fn record_accessibility_snapshot(
        &self,
        target: AccessibilitySnapshotTarget,
        nodes: &[AccessibilityNode],
    ) -> String {
        self.accessibility_snapshots
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .record(target, nodes)
    }

    fn invalidate_accessibility_snapshot(&self, target: &AccessibilitySnapshotTarget) {
        self.accessibility_snapshots
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .invalidate(target);
    }

    fn accessibility_snapshot(
        &self,
        observation_id: Option<&str>,
    ) -> std::result::Result<AccessibilitySnapshot, String> {
        let observation_id = observation_id
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .ok_or_else(|| {
                "observation_id is required for element-based actions. Call get_app_state and pass the returned observation_id."
                    .to_string()
            })?;
        self.accessibility_snapshots
            .lock()
            .map_err(|_| {
                "Could not read accessibility observations. Call get_app_state and retry."
                    .to_string()
            })?
            .resolve(observation_id)
    }

    fn observation_window_id(
        &self,
        observation_id: Option<&str>,
    ) -> std::result::Result<Option<u64>, String> {
        self.accessibility_snapshot(observation_id)
            .map(|snapshot| snapshot.window_id())
    }

    fn resolve_object_ref(
        &self,
        observation_id: Option<&str>,
        element_index: Option<u32>,
        element_identifier: Option<&str>,
        selector: &ElementSelector<'_>,
        purpose: ElementResolvePurpose,
    ) -> std::result::Result<String, String> {
        self.resolve_observed_node(
            observation_id,
            element_index,
            element_identifier,
            selector,
            purpose,
        )
        .map(|node| node.object_ref)
    }

    fn resolve_observed_node(
        &self,
        observation_id: Option<&str>,
        element_index: Option<u32>,
        element_identifier: Option<&str>,
        selector: &ElementSelector<'_>,
        purpose: ElementResolvePurpose,
    ) -> std::result::Result<AccessibilityNode, String> {
        let snapshot = self.accessibility_snapshot(observation_id)?;
        let nodes = snapshot.nodes();
        if let Some(element_identifier) = element_identifier
            .map(str::trim)
            .filter(|value| !value.is_empty())
        {
            let node = nodes
                .iter()
                .find(|node| node.object_ref == element_identifier)
                .cloned()
                .ok_or_else(|| {
                    "element_identifier does not belong to the supplied accessibility observation. Call get_app_state again and use an object_ref from that result."
                        .to_string()
                })?;
            if element_index.is_some_and(|element_index| element_index != node.index) {
                return Err(
                    "element_index and element_identifier select different nodes in the supplied accessibility observation."
                        .to_string(),
                );
            }
            return Ok(node);
        }
        if let Some(element_index) = element_index {
            return nodes
                .iter()
                .find(|node| node.index == element_index)
                .cloned()
                .ok_or_else(|| {
                    format!(
                        "No accessibility node for element_index {element_index} exists in the supplied observation. Call get_app_state again."
                    )
                });
        }
        if selector.is_empty() {
            return Err(
                "Pass element_index, element_identifier, or a semantic selector such as role/name/text/states from the supplied get_app_state result."
                    .to_string(),
            );
        }
        resolve_semantic_node(nodes, selector, purpose)
    }

    async fn perform_element_action(&self, params: &ActionParams) -> Json<ActionOutput> {
        let received = Some(serde_json::json!(params.clone()));
        let node = match self.resolve_observed_node(
            Some(params.observation_id.as_str()),
            params.element_index,
            params.element_identifier.as_deref(),
            &params.selector(),
            ElementResolvePurpose::Action,
        ) {
            Ok(node) => node,
            Err(message) => {
                return Json(ActionOutput {
                    ok: false,
                    implemented: true,
                    action: "perform_action".to_string(),
                    message,
                    received,
                });
            }
        };
        let action_identity =
            match snapshot_action_identity(&node.actions, params.action.as_deref()) {
                Ok(action_identity) => action_identity,
                Err(message) => {
                    return Json(ActionOutput {
                        ok: false,
                        implemented: true,
                        action: "perform_action".to_string(),
                        message,
                        received,
                    });
                }
            };

        match perform_action_by_identity(&node.object_ref, &action_identity).await {
            Ok(invocation) => Json(ActionOutput {
                ok: invocation.ok,
                implemented: true,
                action: "perform_action".to_string(),
                message: if invocation.ok {
                    format!(
                        "AT-SPI action {} ({}) invoked.",
                        invocation.action_index,
                        invocation
                            .action_name
                            .as_deref()
                            .filter(|name| !name.is_empty())
                            .unwrap_or("unnamed")
                    )
                } else {
                    format!(
                        "AT-SPI action {} ({}) returned false.",
                        invocation.action_index,
                        invocation
                            .action_name
                            .as_deref()
                            .filter(|name| !name.is_empty())
                            .unwrap_or("unnamed")
                    )
                },
                received,
            }),
            Err(error) => Json(ActionOutput {
                ok: false,
                implemented: true,
                action: "perform_action".to_string(),
                message: error.to_string(),
                received,
            }),
        }
    }
}

#[derive(Debug)]
enum ClickTarget {
    Coordinates(i32, i32),
    ObservedCoordinates(click_target::ObservedClickTarget),
    ObservedAction(click_target::ObservedClickAction),
}

#[derive(Debug, Clone, Copy)]
enum ElementResolvePurpose {
    ObservedClick,
    ObservedSemanticClick,
    Action,
    SetValue,
}

#[derive(Debug, Clone, Copy, Default)]
struct ElementSelector<'a> {
    role: Option<&'a str>,
    name: Option<&'a str>,
    text: Option<&'a str>,
    states: &'a [String],
}

impl ElementSelector<'_> {
    fn is_empty(&self) -> bool {
        [self.role, self.name, self.text]
            .into_iter()
            .all(|value| value.map(str::trim).is_none_or(str::is_empty))
            && self.states.iter().all(|value| value.trim().is_empty())
    }
}

fn resolve_semantic_node(
    nodes: &[AccessibilityNode],
    selector: &ElementSelector<'_>,
    purpose: ElementResolvePurpose,
) -> std::result::Result<AccessibilityNode, String> {
    let mut matches = nodes
        .iter()
        .filter(|node| node_matches_selector(node, selector))
        .collect::<Vec<_>>();

    if matches.is_empty() {
        return Err(format!(
            "No cached accessibility node matched semantic selector {}. Call get_app_state first or pass element_index.",
            describe_selector(selector)
        ));
    }

    if let Some(node) =
        unique_preferred_node(&matches, |node| node_matches_resolve_purpose(node, purpose))
    {
        return Ok(node.clone());
    }

    let useful_matches = matches
        .iter()
        .copied()
        .filter(|node| node_matches_resolve_purpose(node, purpose))
        .collect::<Vec<_>>();
    if !useful_matches.is_empty() {
        matches = useful_matches;
    }

    if let Some(node) = unique_preferred_node(&matches, node_is_showing) {
        return Ok(node.clone());
    }

    let visible_matches = matches
        .iter()
        .copied()
        .filter(|node| node_is_showing(node))
        .collect::<Vec<_>>();
    if !visible_matches.is_empty() {
        matches = visible_matches;
    }

    if matches.len() == 1 {
        return Ok(matches[0].clone());
    }

    Err(format!(
        "Semantic selector {} matched multiple cached nodes: {}. Pass element_index or add more selector fields.",
        describe_selector(selector),
        describe_matching_nodes(&matches),
    ))
}

fn unique_preferred_node<'a>(
    nodes: &[&'a AccessibilityNode],
    predicate: impl Fn(&AccessibilityNode) -> bool,
) -> Option<&'a AccessibilityNode> {
    let mut matches = nodes.iter().copied().filter(|node| predicate(node));
    let first = matches.next()?;
    matches.next().is_none().then_some(first)
}

fn node_matches_selector(node: &AccessibilityNode, selector: &ElementSelector<'_>) -> bool {
    selector
        .role
        .is_none_or(|role| normalized_contains(Some(node.role.as_str()), role))
        && selector
            .name
            .is_none_or(|name| normalized_contains(node.name.as_deref(), name))
        && selector.text.is_none_or(|text| {
            normalized_contains(
                node.text
                    .as_ref()
                    .and_then(|value| value.content.as_deref()),
                text,
            ) || normalized_contains(node.name.as_deref(), text)
                || normalized_contains(node.description.as_deref(), text)
        })
        && selector
            .states
            .iter()
            .filter(|state| !state.trim().is_empty())
            .all(|state| {
                node.states
                    .iter()
                    .any(|node_state| normalized_equals(node_state, state))
            })
}

fn node_matches_resolve_purpose(node: &AccessibilityNode, purpose: ElementResolvePurpose) -> bool {
    match purpose {
        ElementResolvePurpose::ObservedClick => {
            node.bounds.as_ref().and_then(bounds_center).is_some()
        }
        ElementResolvePurpose::ObservedSemanticClick => {
            node.bounds.as_ref().and_then(bounds_center).is_some()
                || node
                    .actions
                    .first()
                    .is_some_and(|action| action.name.trim().eq_ignore_ascii_case("click"))
        }
        ElementResolvePurpose::Action => !node.actions.is_empty(),
        ElementResolvePurpose::SetValue => node.supports_editable_text || node.value.is_some(),
    }
}

fn node_is_showing(node: &AccessibilityNode) -> bool {
    node.states
        .iter()
        .any(|state| normalized_equals(state, "showing"))
        && node
            .states
            .iter()
            .any(|state| normalized_equals(state, "visible"))
}

fn normalized_equals(actual: &str, expected: &str) -> bool {
    normalize_text(actual) == normalize_text(expected)
}

fn normalized_contains(actual: Option<&str>, expected: &str) -> bool {
    let expected = normalize_text(expected);
    !expected.is_empty()
        && actual
            .map(normalize_text)
            .is_some_and(|actual| actual.contains(&expected))
}

fn normalize_text(value: &str) -> String {
    value
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .to_lowercase()
}

fn describe_selector(selector: &ElementSelector<'_>) -> String {
    let mut parts = Vec::new();
    if let Some(role) = selector.role.map(str::trim).filter(|role| !role.is_empty()) {
        parts.push(format!("role={role:?}"));
    }
    if let Some(name) = selector.name.map(str::trim).filter(|name| !name.is_empty()) {
        parts.push(format!("name={name:?}"));
    }
    if let Some(text) = selector.text.map(str::trim).filter(|text| !text.is_empty()) {
        parts.push(format!("text={text:?}"));
    }
    let states = selector
        .states
        .iter()
        .map(|state| state.trim())
        .filter(|state| !state.is_empty())
        .collect::<Vec<_>>();
    if !states.is_empty() {
        parts.push(format!("states={states:?}"));
    }
    if parts.is_empty() {
        "<empty>".to_string()
    } else {
        parts.join(", ")
    }
}

fn describe_matching_nodes(nodes: &[&AccessibilityNode]) -> String {
    nodes
        .iter()
        .take(8)
        .map(|node| {
            format!(
                "element_index {} role={:?} name={:?}",
                node.index, node.role, node.name
            )
        })
        .collect::<Vec<_>>()
        .join("; ")
}

fn is_plain_left_click(button: Option<&str>, click_count: Option<u32>) -> bool {
    let button = button.unwrap_or("left");
    let click_count = click_count.unwrap_or(1);
    matches!(button.to_ascii_lowercase().as_str(), "left" | "primary") && click_count == 1
}

fn snapshot_action_identity(
    actions: &[AccessibilityAction],
    requested_action: Option<&str>,
) -> std::result::Result<ActionFingerprint, String> {
    let requested_action = requested_action
        .map(str::trim)
        .filter(|value| !value.is_empty());
    let action = match requested_action {
        None => actions.first(),
        Some(requested_action) => {
            let mut text_matches = actions.iter().filter(|action| {
                action.name.trim().eq_ignore_ascii_case(requested_action)
                    || action
                        .description
                        .trim()
                        .eq_ignore_ascii_case(requested_action)
            });
            let text_match = text_matches.next();
            if text_matches.next().is_some() {
                return Err(
                    "The requested AT-SPI action is ambiguous in the supplied accessibility observation."
                        .to_string(),
                );
            }
            text_match.or_else(|| {
                requested_action
                    .parse::<i32>()
                    .ok()
                    .and_then(|index| actions.iter().find(|action| action.index == index))
            })
        }
    }
    .ok_or_else(|| {
        "The requested AT-SPI action was not present in the supplied accessibility observation. Call get_app_state again."
            .to_string()
    })?;

    ActionFingerprint::new(&action.name, &action.description).ok_or_else(|| {
        "The selected AT-SPI action has no stable textual identity, so it cannot be invoked safely."
            .to_string()
    })
}

fn bounds_center(bounds: &Bounds) -> Option<(i32, i32)> {
    if bounds.width <= 0 || bounds.height <= 0 {
        return None;
    }
    if bounds.x <= i32::MIN / 2 || bounds.y <= i32::MIN / 2 {
        return None;
    }
    Some((
        bounds.x.checked_add(bounds.width / 2)?,
        bounds.y.checked_add(bounds.height / 2)?,
    ))
}

fn compact_accessibility_tree(nodes: Vec<AccessibilityNode>) -> Vec<AccessibilityNode> {
    if nodes.is_empty() {
        return nodes;
    }

    let keep = nodes
        .iter()
        .map(should_keep_accessibility_node)
        .collect::<Vec<_>>();
    let mut old_to_new = vec![None; nodes.len()];
    let mut compacted = Vec::new();

    for (old_index, node) in nodes.iter().enumerate() {
        if !keep[old_index] {
            continue;
        }

        let mut compacted_node = node.clone();
        compacted_node.index = compacted.len() as u32;
        compacted_node.parent_index = nearest_kept_parent(&keep, &nodes, old_index);
        old_to_new[old_index] = Some(compacted_node.index);
        compacted.push(compacted_node);
    }

    for node in &mut compacted {
        node.parent_index = node
            .parent_index
            .and_then(|old_parent| old_to_new.get(old_parent as usize).copied().flatten());
    }

    let child_counts = compacted.iter().filter_map(|node| node.parent_index).fold(
        vec![0_i32; compacted.len()],
        |mut counts, parent_index| {
            counts[parent_index as usize] += 1;
            counts
        },
    );

    for (index, node) in compacted.iter_mut().enumerate() {
        node.child_count = child_counts[index];
    }

    compacted
}

fn nearest_kept_parent(
    keep: &[bool],
    nodes: &[AccessibilityNode],
    old_index: usize,
) -> Option<u32> {
    let mut parent = nodes[old_index].parent_index;
    while let Some(parent_index) = parent {
        let parent_usize = parent_index as usize;
        if keep.get(parent_usize).copied().unwrap_or(false) {
            return Some(parent_index);
        }
        parent = nodes.get(parent_usize).and_then(|node| node.parent_index);
    }
    None
}

fn should_keep_accessibility_node(node: &AccessibilityNode) -> bool {
    if node.depth <= 1 {
        return true;
    }

    if is_actionable_accessibility_node(node) || has_meaningful_node_copy(node) {
        return true;
    }

    matches!(
        node.role.as_str(),
        "page tab" | "menu item" | "menu" | "list item" | "tree item"
    ) && !is_sentinel_or_missing_bounds(node.bounds.as_ref())
}

fn is_actionable_accessibility_node(node: &AccessibilityNode) -> bool {
    !node.actions.is_empty() || node.supports_editable_text || node.value.is_some()
}

fn has_meaningful_node_copy(node: &AccessibilityNode) -> bool {
    has_non_empty_text(node.name.as_deref())
        || has_non_empty_text(node.description.as_deref())
        || has_non_empty_text(node.text.as_ref().and_then(|text| text.content.as_deref()))
}

fn has_non_empty_text(value: Option<&str>) -> bool {
    value.map(str::trim).is_some_and(|value| !value.is_empty())
}

fn is_sentinel_or_missing_bounds(bounds: Option<&Bounds>) -> bool {
    bounds.is_none()
}

fn select_accessibility_object_ref(
    apps: &[AccessibleAppSummary],
    target_pid: u32,
    candidates: &[String],
) -> Option<String> {
    let mut pid_matches = apps.iter().filter(|app| app.pid == Some(target_pid));
    let first = pid_matches.next()?;
    let second = pid_matches.next();

    if second.is_none() {
        return Some(first.object_ref.clone());
    }

    let lowered_candidates = candidates
        .iter()
        .map(|candidate| candidate.to_ascii_lowercase())
        .collect::<Vec<_>>();

    apps.iter()
        .filter(|app| app.pid == Some(target_pid))
        .find(|app| {
            let name = app.name.as_deref().unwrap_or_default().to_ascii_lowercase();
            lowered_candidates
                .iter()
                .any(|candidate| !candidate.is_empty() && name.contains(candidate))
        })
        .map(|app| app.object_ref.clone())
        .or_else(|| Some(first.object_ref.clone()))
}

fn accessibility_filter_candidates(window_context: Option<&WindowInfo>) -> Vec<String> {
    let Some(window) = window_context else {
        return Vec::new();
    };

    let mut candidates = Vec::new();
    push_candidate(&mut candidates, window.title.as_deref());
    push_candidate(&mut candidates, window.wm_class.as_deref());

    if let Some(app_id) = trimmed_nonempty(window.app_id.as_deref()) {
        if !app_id.starts_with("window:") {
            push_candidate(&mut candidates, Some(app_id));
            if let Some(stripped) = app_id.strip_suffix(".desktop") {
                push_candidate(&mut candidates, Some(stripped));
                let normalized = stripped.replace(['-', '_', '.'], " ");
                push_candidate(&mut candidates, Some(normalized.as_str()));
            } else {
                let normalized = app_id.replace(['-', '_', '.'], " ");
                push_candidate(&mut candidates, Some(normalized.as_str()));
            }
        }
    }

    candidates
}

fn accessibility_snapshot_target(
    params: &GetAppStateParams,
    window_context: Option<&WindowInfo>,
) -> Option<AccessibilitySnapshotTarget> {
    if let Some(window) = window_context {
        return Some(AccessibilitySnapshotTarget::Window {
            window_id: window.window_id,
            pid: window.pid,
        });
    }
    if let Some(application) = trimmed_nonempty(params.app_name_or_bundle_identifier.as_deref()) {
        return Some(AccessibilitySnapshotTarget::application(application));
    }
    if let Some(pid) = params.pid {
        return Some(AccessibilitySnapshotTarget::Process(pid));
    }
    if params.window_target().has_target() {
        return None;
    }
    Some(AccessibilitySnapshotTarget::Desktop)
}

fn push_candidate(candidates: &mut Vec<String>, value: Option<&str>) {
    let Some(value) = trimmed_nonempty(value) else {
        return;
    };

    if !candidates.iter().any(|candidate| candidate == value) {
        candidates.push(value.to_string());
    }
}

fn trimmed_nonempty(value: Option<&str>) -> Option<&str> {
    value.map(str::trim).filter(|value| !value.is_empty())
}

fn env_contains(key: &str, needle: &str) -> bool {
    env::var(key)
        .ok()
        .is_some_and(|value| value.to_ascii_lowercase().contains(needle))
}

/// True when an environment variable is set to `"1"` (an explicit on switch).
fn env_flag_enabled(key: &str) -> bool {
    env::var(key).ok().as_deref() == Some("1")
}

/// Return the base64 payload of a `data:` URL (or the original string if bare).
fn data_url_payload(data_url: &str) -> String {
    data_url
        .split_once(',')
        .map(|(_, payload)| payload)
        .unwrap_or(data_url)
        .to_string()
}

fn screenshot_failure(
    stage: &'static str,
    target: Option<&WindowTarget>,
    error: impl std::fmt::Display,
) -> ErrorData {
    let data = target.map(|target| {
        let mut selector_fields = Vec::new();
        for (name, present) in [
            ("tty", target.tty.is_some()),
            ("terminal_command", target.terminal_command.is_some()),
            ("terminal_cwd", target.terminal_cwd.is_some()),
            ("app_id", target.app_id.is_some()),
            ("wm_class", target.wm_class.is_some()),
            ("title", target.title.is_some()),
        ] {
            if present {
                selector_fields.push(name);
            }
        }
        serde_json::json!({
            "stage": stage,
            "target": {
                "window_id": target.window_id,
                "pid": target.pid,
                "terminal_pid": target.terminal_pid,
                "selector_fields": selector_fields,
            },
            "image_returned": false,
        })
    });
    let error = bounded_screenshot_error(error);
    ErrorData::internal_error(format!("screenshot {stage} failed: {error}"), data)
}

fn bounded_screenshot_error(error: impl std::fmt::Display) -> String {
    const MAX_CHARS: usize = 512;
    let error = error.to_string();
    let mut chars = error.chars();
    let mut bounded = chars.by_ref().take(MAX_CHARS).collect::<String>();
    if chars.next().is_some() {
        bounded.push('…');
    }
    bounded
}

fn session_is_wayland(session_type: Option<&str>, wayland_display: Option<&str>) -> bool {
    match session_type {
        Some(value) => value.eq_ignore_ascii_case("wayland"),
        None => wayland_display.is_some_and(|value| !value.is_empty()),
    }
}

fn validated_target_bounds(window: Option<&WindowInfo>) -> Result<crate::windowing::WindowBounds> {
    let bounds = window
        .and_then(|window| window.bounds.clone())
        .ok_or_else(|| {
            anyhow::anyhow!(
                "targeted screenshot requires resolved window bounds; refusing to capture the full desktop"
            )
        })?;
    window_crop_rect(&bounds).ok_or_else(|| {
        anyhow::anyhow!(
            "targeted screenshot has unusable window bounds; refusing to capture the full desktop"
        )
    })?;
    Ok(bounds)
}

fn crop_raw_screenshot(
    mut raw: RawScreenshotCapture,
    bounds: Option<&crate::windowing::WindowBounds>,
    target_requested: bool,
) -> Result<(RawScreenshotCapture, Option<(u32, u32)>)> {
    if target_requested && bounds.is_none() {
        anyhow::bail!(
            "targeted screenshot requires a resolved window; refusing to return the full desktop"
        );
    }
    if let Some(bounds) = bounds {
        let (mut x, mut y, mut width, mut height) = window_crop_rect(bounds).ok_or_else(|| {
            anyhow::anyhow!(
                "targeted screenshot has unusable window bounds; refusing to return the full desktop"
            )
        })?;
        let mut origin_x = 0;
        let mut origin_y = 0;
        if x < 0 {
            origin_x = x.unsigned_abs();
            width = width.saturating_sub(origin_x);
            x = 0;
        }
        if y < 0 {
            origin_y = y.unsigned_abs();
            height = height.saturating_sub(origin_y);
            y = 0;
        }
        if width == 0 || height == 0 {
            anyhow::bail!(
                "targeted screenshot window is outside the captured desktop; refusing to return the full desktop"
            );
        }
        let (bytes, width, height) = crop_png(&raw.bytes, x, y, width, height)
            .map_err(|error| anyhow::anyhow!("targeted screenshot crop failed: {error}"))?;
        raw = RawScreenshotCapture {
            mime_type: raw.mime_type,
            bytes,
            source: raw.source,
            width,
            height,
        };
        return Ok((raw, Some((origin_x, origin_y))));
    }
    Ok((raw, None))
}

/// Convert a window's bounds into a crop rectangle, if it has a usable origin
/// and non-zero size.
fn point_in_addressable_desktop(
    point: (i32, i32),
    capture_rect: (i32, i32, i32, i32),
    logical_rects: &[(i32, i32, i32, i32)],
) -> bool {
    std::iter::once(&capture_rect)
        .chain(logical_rects)
        .any(|&(x, y, width, height)| {
            let (point_x, point_y) = (i64::from(point.0), i64::from(point.1));
            let (x, y, width, height) = (
                i64::from(x),
                i64::from(y),
                i64::from(width),
                i64::from(height),
            );
            width > 0
                && height > 0
                && point_x >= x
                && point_y >= y
                && point_x < x + width
                && point_y < y + height
        })
}

fn window_crop_rect(bounds: &crate::windowing::WindowBounds) -> Option<(i32, i32, u32, u32)> {
    let x = bounds.x?;
    let y = bounds.y?;
    if bounds.width == 0 || bounds.height == 0 {
        return None;
    }
    Some((x, y, bounds.width, bounds.height))
}

fn validate_claimed_window_point(
    claim: &ClaimContext,
    focus: Option<&WindowFocusResult>,
    point: (i32, i32),
    action: &str,
) -> std::result::Result<(), String> {
    if claim.owner_thread_id.is_none() || claim.claim_token.is_none() {
        return Ok(());
    }
    let bounds = focus
        .and_then(|focus| {
            focus
                .focused_window
                .as_ref()
                .and_then(|window| window.bounds.as_ref())
                .or(focus.requested_window.bounds.as_ref())
        })
        .and_then(window_crop_rect)
        .ok_or_else(|| format!("Claimed {action} requires verified target-window bounds."))?;
    let (x, y, width, height) = bounds;
    let inside = i64::from(point.0) >= i64::from(x)
        && i64::from(point.1) >= i64::from(y)
        && i64::from(point.0) < i64::from(x) + i64::from(width)
        && i64::from(point.1) < i64::from(y) + i64::from(height);
    if inside {
        Ok(())
    } else {
        Err(format!(
            "Claimed {action} coordinates must be inside the authorized target window."
        ))
    }
}

fn apply_window_relative_click_coordinates(
    params: &mut ClickParams,
    focus: &WindowFocusResult,
) -> std::result::Result<(), String> {
    let (relative_x, relative_y) = params
        .x
        .zip(params.y)
        .ok_or_else(|| "Relative coordinate clicks require both x and y.".to_string())?;
    let bounds = focus
        .focused_window
        .as_ref()
        .and_then(|window| window.bounds.as_ref())
        .or(focus.requested_window.bounds.as_ref())
        .ok_or_else(|| {
            "Relative coordinate clicks require resolved target-window bounds.".to_string()
        })?;
    if bounds.width == 0 || bounds.height == 0 {
        return Err(
            "Relative coordinate clicks require non-empty target-window bounds.".to_string(),
        );
    }
    if relative_x < 0 || relative_y < 0 {
        return Err("Relative click coordinates must be inside target-window bounds.".to_string());
    }
    if relative_x as u32 >= bounds.width || relative_y as u32 >= bounds.height {
        return Err("Relative click coordinates must be inside target-window bounds.".to_string());
    }
    let (origin_x, origin_y) = bounds.x.zip(bounds.y).ok_or_else(|| {
        "Relative coordinate clicks require target-window bounds with an origin.".to_string()
    })?;
    let x = origin_x
        .checked_add(relative_x)
        .ok_or_else(|| "Relative click x coordinate overflowed.".to_string())?;
    let y = origin_y
        .checked_add(relative_y)
        .ok_or_else(|| "Relative click y coordinate overflowed.".to_string())?;
    params.x = Some(x);
    params.y = Some(y);
    Ok(())
}

/// Point a window-targeted scroll at the centre of the resolved window when
/// the caller supplied no coordinates. Without this the wheel events land on
/// whatever is under the current pointer position.
fn apply_window_center_scroll_point(
    params: &mut ScrollParams,
    focus: &WindowFocusResult,
) -> std::result::Result<(), String> {
    let bounds = focus
        .focused_window
        .as_ref()
        .and_then(|window| window.bounds.as_ref())
        .or(focus.requested_window.bounds.as_ref())
        .ok_or_else(|| {
            "Window-targeted scroll requires resolved target-window bounds; pass x/y explicitly."
                .to_string()
        })?;
    if bounds.width == 0 || bounds.height == 0 {
        return Err(
            "Window-targeted scroll requires non-empty target-window bounds; pass x/y explicitly."
                .to_string(),
        );
    }
    let (origin_x, origin_y) = bounds.x.zip(bounds.y).ok_or_else(|| {
        "Window-targeted scroll requires target-window bounds with an origin; pass x/y explicitly."
            .to_string()
    })?;
    params.x = Some(origin_x.saturating_add((bounds.width / 2) as i32));
    params.y = Some(origin_y.saturating_add((bounds.height / 2) as i32));
    Ok(())
}

fn apply_window_relative_scroll_coordinates(
    params: &mut ScrollParams,
    focus: &WindowFocusResult,
) -> std::result::Result<(), String> {
    let (relative_x, relative_y) = params
        .x
        .zip(params.y)
        .ok_or_else(|| "Relative scroll coordinates require both x and y.".to_string())?;
    let bounds = focus
        .focused_window
        .as_ref()
        .and_then(|window| window.bounds.as_ref())
        .or(focus.requested_window.bounds.as_ref())
        .ok_or_else(|| {
            "Relative scroll coordinates require resolved target-window bounds.".to_string()
        })?;
    if bounds.width == 0 || bounds.height == 0 {
        return Err(
            "Relative scroll coordinates require non-empty target-window bounds.".to_string(),
        );
    }
    if relative_x < 0
        || relative_y < 0
        || relative_x as u32 >= bounds.width
        || relative_y as u32 >= bounds.height
    {
        return Err("Relative scroll coordinates must be inside target-window bounds.".to_string());
    }
    let (origin_x, origin_y) = bounds.x.zip(bounds.y).ok_or_else(|| {
        "Relative scroll coordinates require target-window bounds with an origin.".to_string()
    })?;
    params.x = Some(origin_x.saturating_add(relative_x));
    params.y = Some(origin_y.saturating_add(relative_y));
    Ok(())
}

/// Crop a PNG image to `(x, y, w, h)` (clamped to the image), returning the
/// re-encoded PNG and the actual cropped dimensions.
fn crop_png(
    raw: &[u8],
    x: i32,
    y: i32,
    w: u32,
    h: u32,
) -> std::result::Result<(Vec<u8>, u32, u32), String> {
    use std::io::Cursor;
    let img = image::load_from_memory_with_format(raw, image::ImageFormat::Png)
        .map_err(|e| format!("decode png: {e}"))?;
    let (iw, ih) = (img.width(), img.height());
    let x = x.max(0) as u32;
    let y = y.max(0) as u32;
    if x >= iw || y >= ih {
        return Err("crop origin outside image".into());
    }
    let w = w.min(iw - x);
    let h = h.min(ih - y);
    let sub = img.crop_imm(x, y, w, h);
    let mut out = Vec::new();
    sub.write_to(&mut Cursor::new(&mut out), image::ImageFormat::Png)
        .map_err(|e| format!("encode png: {e}"))?;
    Ok((out, w, h))
}

fn action_result(
    action: &str,
    result: std::result::Result<Vec<Output>, String>,
    received: Option<serde_json::Value>,
) -> ActionOutput {
    match result {
        Ok(_) => ActionOutput {
            ok: true,
            implemented: true,
            action: action.to_string(),
            message: "Action sent through ydotool.".to_string(),
            received,
        },
        Err(message) => ActionOutput {
            ok: false,
            implemented: true,
            action: action.to_string(),
            message,
            received,
        },
    }
}

fn action_error(
    action: &str,
    message: String,
    received: Option<serde_json::Value>,
) -> Json<ActionOutput> {
    Json(ActionOutput {
        ok: false,
        implemented: true,
        action: action.to_string(),
        message,
        received,
    })
}

fn portal_action_delivery_failure(
    action: &str,
    error: &PortalActionError,
    received: Option<serde_json::Value>,
) -> ActionOutput {
    ActionOutput {
        ok: false,
        implemented: true,
        action: action.to_string(),
        message: format!(
            "Remote desktop portal {action} failed after the action attempt began. It may have been partially delivered, so it was not replayed through ydotool: {error:#}"
        ),
        received,
    }
}

fn portal_coordinate_input_unavailable(
    action: &str,
    received: Option<serde_json::Value>,
) -> ActionOutput {
    ActionOutput {
        ok: false,
        implemented: true,
        action: action.to_string(),
        message: format!(
            "Did not send {action} input: the RemoteDesktop portal exposes no spec-defined transform from screenshot pixels to absolute pointer coordinates, and an allowed working ydotool backend is unavailable."
        ),
        received,
    }
}

fn action_result_with_focus(
    action: &str,
    result: std::result::Result<Vec<Output>, String>,
    received: Option<serde_json::Value>,
    focus: Option<WindowFocusResult>,
) -> ActionOutput {
    with_focus_context(action_result(action, result, received), focus)
}

fn successful_action_with_focus(
    action: &str,
    message: &str,
    received: Option<serde_json::Value>,
    focus: Option<WindowFocusResult>,
) -> ActionOutput {
    with_focus_context(
        ActionOutput {
            ok: true,
            implemented: true,
            action: action.to_string(),
            message: message.to_string(),
            received,
        },
        focus,
    )
}

fn with_focus_context(mut output: ActionOutput, focus: Option<WindowFocusResult>) -> ActionOutput {
    if output.ok {
        if let Some(focus) = focus {
            let verification = if focus.exact_window_focused {
                "exact window-focus"
            } else {
                "app-level focus"
            };
            output.message = format!(
                "{} Target window_id {} was focused with {verification} verification before input.",
                output.message, focus.requested_window.window_id,
            );
        }
    }
    output
}

fn describe_focused_element(element: &FocusedElementSummary, expects_editable: bool) -> String {
    let name = element
        .name
        .as_deref()
        .filter(|name| !name.is_empty())
        .map(|name| format!(" \"{name}\""))
        .unwrap_or_default();
    if element.editable {
        format!("Focused element: {}{name} (editable).", element.role)
    } else if expects_editable {
        format!(
            "WARNING: focused element is {}{name}, which is not editable — {NON_EDITABLE_TEXT_LANDING_WARNING}. Click the intended input first or use set_value.",
            element.role
        )
    } else {
        format!("Focused element: {}{name} (not editable).", element.role)
    }
}

fn first_line(text: &str) -> &str {
    text.lines().next().unwrap_or(text)
}

/// Append supplemental notes (off-screen or focused-element feedback) to an
/// action result message without changing ok/implemented semantics.
fn with_notes(mut output: ActionOutput, notes: impl IntoIterator<Item = String>) -> ActionOutput {
    for note in notes {
        output.message = format!("{} {note}", output.message);
    }
    output
}

fn focus_satisfies_target(focus: &WindowFocusResult, target: &WindowTarget) -> bool {
    if target.requires_exact_focus() {
        focus.exact_window_focused
    } else {
        focus.exact_window_focused || focus.app_focused
    }
}

async fn window_list_output() -> ListWindowsOutput {
    match list_windows().await {
        Ok(windows) => {
            let backend = window_backend(windows.iter());
            let note = registry::list_note(&backend);
            ListWindowsOutput {
                backend,
                windows,
                error: None,
                permissions_hint: None,
                note: note.to_string(),
            }
        }
        Err(error) => {
            let error = format!("{error:#}");
            ListWindowsOutput {
                backend: GNOME_SHELL_INTROSPECT_BACKEND.to_string(),
                windows: Vec::new(),
                permissions_hint: window_permission_hint(&error),
                error: Some(error),
                note: "Window listing failed, so targeted keyboard input cannot safely focus or verify a target window."
                    .to_string(),
            }
        }
    }
}

fn window_backend<'a>(windows: impl Iterator<Item = &'a WindowInfo>) -> String {
    windows
        .map(|window| window.backend.clone())
        .next()
        .unwrap_or_else(|| GNOME_SHELL_INTROSPECT_BACKEND.to_string())
}

fn absolute_mousemove_args(x: i32, y: i32) -> Vec<String> {
    vec![
        "mousemove".to_string(),
        "--absolute".to_string(),
        "--".to_string(),
        x.to_string(),
        y.to_string(),
    ]
}

fn wheel_mousemove_args(dx: i32, dy: i32) -> Vec<String> {
    vec![
        "mousemove".to_string(),
        "--wheel".to_string(),
        "--".to_string(),
        dx.to_string(),
        dy.to_string(),
    ]
}

async fn run_ydotool_sequence(
    commands: &[Vec<String>],
) -> std::result::Result<Vec<Output>, String> {
    let mut outputs = Vec::new();
    for (index, args) in commands.iter().enumerate() {
        outputs.push(run_ydotool(args).await?);
        if index + 1 < commands.len() {
            sleep(Duration::from_millis(35)).await;
        }
    }
    Ok(outputs)
}

async fn run_ydotool(args: &[String]) -> std::result::Result<Output, String> {
    ydotool::ensure_supported()?;
    let mut command = TokioCommand::new("ydotool");
    command.args(args);
    if let Some(socket) = ydotool_socket() {
        command.env("YDOTOOL_SOCKET", socket);
    }
    command.stdout(Stdio::piped());
    command.stderr(Stdio::piped());

    match command.spawn() {
        Ok(child) => match wait_for_ydotool_output(child).await {
            Ok(output) if output.status.success() => {
                if let Some(error) = ydotool::cli_error(&output.stderr) {
                    Err(error)
                } else {
                    Ok(output)
                }
            }
            Ok(output) => Err(ydotool_output_error(output)),
            Err(error) => Err(error),
        },
        Err(error) => Err(format!("failed to run ydotool: {error}")),
    }
}

async fn run_ydotool_type_text(text: &str) -> std::result::Result<Output, String> {
    ydotool::ensure_supported()?;
    let mut command = TokioCommand::new("ydotool");
    command.args(["type", "--file", "-"]);
    if let Some(socket) = ydotool_socket() {
        command.env("YDOTOOL_SOCKET", socket);
    }
    command.stdin(Stdio::piped());
    command.stdout(Stdio::piped());
    command.stderr(Stdio::piped());

    match command.spawn() {
        Ok(mut child) => {
            if let Some(mut stdin) = child.stdin.take() {
                if let Err(error) = stdin.write_all(text.as_bytes()).await {
                    let _ = child.kill().await;
                    return Err(format!("failed to write text to ydotool stdin: {error}"));
                }
            }
            let output =
                wait_for_ydotool_output_with_timeout(child, ydotool_type_timeout(text)).await?;
            if output.status.success() {
                if let Some(error) = ydotool::cli_error(&output.stderr) {
                    Err(error)
                } else {
                    Ok(output)
                }
            } else {
                Err(ydotool_output_error(output))
            }
        }
        Err(error) => Err(format!("failed to run ydotool: {error}")),
    }
}

async fn wait_for_ydotool_output(child: TokioChild) -> std::result::Result<Output, String> {
    wait_for_ydotool_output_with_timeout(child, YDOTOOL_TIMEOUT).await
}

async fn wait_for_ydotool_output_with_timeout(
    mut child: TokioChild,
    timeout_duration: Duration,
) -> std::result::Result<Output, String> {
    let stdout_reader = read_child_pipe(child.stdout.take());
    let stderr_reader = read_child_pipe(child.stderr.take());
    let status = match timeout(timeout_duration, child.wait()).await {
        Err(_) => {
            let _ = child.kill().await;
            let _ = child.wait().await;
            stdout_reader.abort();
            stderr_reader.abort();
            return Err(format!(
                "ydotool timed out after {}s",
                timeout_duration.as_secs()
            ));
        }
        Ok(result) => result.map_err(|error| format!("failed to wait for ydotool: {error}"))?,
    };
    let stdout = stdout_reader.await.unwrap_or_default();
    let stderr = stderr_reader.await.unwrap_or_default();
    Ok(Output {
        status,
        stdout,
        stderr,
    })
}

fn read_child_pipe<R>(pipe: Option<R>) -> tokio::task::JoinHandle<Vec<u8>>
where
    R: AsyncRead + Unpin + Send + 'static,
{
    tokio::spawn(async move {
        let mut output = Vec::new();
        if let Some(mut pipe) = pipe {
            let _ = pipe.read_to_end(&mut output).await;
        }
        output
    })
}

fn ydotool_type_timeout(text: &str) -> Duration {
    let text_seconds = (text.chars().count() as u64).div_ceil(YDOTOOL_TYPE_CHARS_PER_SECOND);
    Duration::from_secs(YDOTOOL_TIMEOUT.as_secs().saturating_add(text_seconds))
}

const EVDEV_KEY_LEFTCTRL: i32 = 29;
const EVDEV_KEY_V: i32 = 47;
const KDE_CLIPBOARD_RESTORE_MIN_DELAY_MS: u64 = 1_500;
const KDE_CLIPBOARD_RESTORE_MAX_DELAY_MS: u64 = 5_000;
const KDE_CLIPBOARD_RESTORE_CHARS_PER_SECOND: u64 = 250;

fn kde_clipboard_restore_delay(text: &str) -> Duration {
    let text_delay_ms = (text.chars().count() as u64)
        .saturating_mul(1_000)
        .div_ceil(KDE_CLIPBOARD_RESTORE_CHARS_PER_SECOND);
    Duration::from_millis(text_delay_ms.clamp(
        KDE_CLIPBOARD_RESTORE_MIN_DELAY_MS,
        KDE_CLIPBOARD_RESTORE_MAX_DELAY_MS,
    ))
}

#[derive(Debug)]
struct KdeClipboardPasteError {
    message: String,
    can_fallback_to_ydotool: bool,
    clear_portal_keyboard_session: bool,
}

impl KdeClipboardPasteError {
    fn before_text_input(message: String) -> Self {
        Self {
            message,
            can_fallback_to_ydotool: true,
            clear_portal_keyboard_session: false,
        }
    }

    fn after_portal_input(message: String) -> Self {
        Self {
            message,
            can_fallback_to_ydotool: false,
            clear_portal_keyboard_session: true,
        }
    }
}

async fn run_kde_clipboard_paste_text(
    session: &PortalKeyboardSession,
    text: &str,
) -> std::result::Result<String, KdeClipboardPasteError> {
    let previous = kde_clipboard_contents()
        .await
        .map_err(KdeClipboardPasteError::before_text_input)?;
    kde_set_clipboard_contents(text)
        .await
        .map_err(KdeClipboardPasteError::before_text_input)?;

    let paste_result = press_keycode_chord(session, &[EVDEV_KEY_LEFTCTRL], EVDEV_KEY_V)
        .await
        .map_err(|error| format!("{error:#}"));

    sleep(kde_clipboard_restore_delay(text)).await;
    let restore_result = kde_set_clipboard_contents(&previous).await;

    match (paste_result, restore_result) {
        (Ok(_), Ok(_)) => Ok("Action pasted through KDE clipboard integration.".to_string()),
        (Err(error), Ok(_)) => Err(KdeClipboardPasteError::after_portal_input(error)),
        (Ok(_), Err(restore_error)) => Ok(format!(
            "Action pasted through KDE clipboard integration. Warning: previous KDE clipboard contents could not be restored: {restore_error}"
        )),
        (Err(error), Err(restore_error)) => Err(KdeClipboardPasteError::after_portal_input(
            format!("{error}; previous KDE clipboard contents could not be restored: {restore_error}"),
        )),
    }
}

async fn kde_clipboard_contents() -> std::result::Result<String, String> {
    let connection = kde_clipboard_connection().await?;
    let proxy = kde_clipboard_proxy(&connection).await?;
    let output: String = kde_clipboard_dbus_operation(
        "getClipboardContents",
        proxy.call("getClipboardContents", &()),
    )
    .await?;
    Ok(output)
}

async fn kde_set_clipboard_contents(text: &str) -> std::result::Result<(), String> {
    let connection = kde_clipboard_connection().await?;
    let proxy = kde_clipboard_proxy(&connection).await?;
    let _: () = kde_clipboard_dbus_operation(
        "setClipboardContents",
        proxy.call("setClipboardContents", &(text)),
    )
    .await?;
    Ok(())
}

async fn kde_clipboard_connection() -> std::result::Result<ZbusConnection, String> {
    ZbusConnection::session()
        .await
        .map_err(|error| format!("failed to connect to session bus for KDE clipboard: {error}"))
}

async fn kde_clipboard_proxy(
    connection: &ZbusConnection,
) -> std::result::Result<ZbusProxy<'_>, String> {
    kde_clipboard_dbus_operation(
        "proxy creation",
        ZbusProxy::new(
            connection,
            KDE_KLIPPER_SERVICE,
            KDE_KLIPPER_PATH,
            KDE_KLIPPER_INTERFACE,
        ),
    )
    .await
}

async fn kde_clipboard_dbus_operation<T, F>(
    operation: &'static str,
    future: F,
) -> std::result::Result<T, String>
where
    F: Future<Output = zbus::Result<T>>,
{
    kde_clipboard_dbus_operation_with_timeout(operation, future, KDE_CLIPBOARD_DBUS_TIMEOUT).await
}

async fn kde_clipboard_dbus_operation_with_timeout<T, F>(
    operation: &'static str,
    future: F,
    timeout_duration: Duration,
) -> std::result::Result<T, String>
where
    F: Future<Output = zbus::Result<T>>,
{
    timeout(timeout_duration, future)
        .await
        .map_err(|_| format!("KDE clipboard {operation} timed out"))?
        .map_err(|error| format!("KDE clipboard {operation} failed: {error}"))
}

fn ydotool_output_error(output: Output) -> String {
    command_output_error("ydotool", output)
}

fn command_output_error(command: &str, output: Output) -> String {
    let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
    let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
    let detail = if stderr.is_empty() { stdout } else { stderr };
    if detail.is_empty() {
        format!("{command} exited with {}", output.status)
    } else {
        detail
    }
}

fn ydotool_socket() -> Option<String> {
    if let Some(socket) = explicit_ydotool_socket() {
        return Some(socket);
    }

    connectable_ydotool_socket_from(fallback_ydotool_socket_candidates())
        .map(|path| path.display().to_string())
}

fn ydotool_backend_available() -> bool {
    ydotool_backend_available_from(
        ydotool_socket_connectable(),
        ydotool::ensure_supported().is_ok(),
    )
}

fn ydotool_socket_connectable() -> bool {
    if let Some(socket) = explicit_ydotool_socket() {
        return ydotool_socket_connects(&PathBuf::from(socket));
    }
    connectable_ydotool_socket_from(fallback_ydotool_socket_candidates()).is_some()
}

fn ydotool_backend_available_from(socket_available: bool, cli_supported: bool) -> bool {
    socket_available && cli_supported
}

fn should_prefer_portal_backend_by_default(is_wayland: bool, ydotool_available: bool) -> bool {
    is_wayland && !ydotool_available
}

fn explicit_ydotool_socket() -> Option<String> {
    if let Ok(socket) = env::var("YDOTOOL_SOCKET") {
        let socket = socket.trim();
        if !socket.is_empty() {
            return Some(socket.to_string());
        }
    }
    None
}

fn fallback_ydotool_socket_candidates() -> Vec<PathBuf> {
    let mut candidates = Vec::new();
    if let Some(runtime) = env::var("XDG_RUNTIME_DIR")
        .ok()
        .map(PathBuf::from)
        .or_else(|| user_id().map(|uid| PathBuf::from(format!("/run/user/{uid}"))))
    {
        candidates.push(runtime.join(".ydotool_socket"));
    }
    candidates.push(PathBuf::from("/tmp/.ydotool_socket"));
    candidates
}

fn connectable_ydotool_socket_from(candidates: Vec<PathBuf>) -> Option<PathBuf> {
    candidates.into_iter().find(ydotool_socket_connects)
}

fn ydotool_socket_connects(path: &PathBuf) -> bool {
    UnixStream::connect(path).is_ok()
        || UnixDatagram::unbound()
            .and_then(|socket| socket.connect(path))
            .is_ok()
}

fn mouse_button_code(button: Option<&str>) -> String {
    match button.unwrap_or("left").to_ascii_lowercase().as_str() {
        "right" => "0xC1",
        "middle" => "0xC2",
        "side" => "0xC3",
        "extra" => "0xC4",
        "forward" => "0xC5",
        "back" => "0xC6",
        _ => "0xC0",
    }
    .to_string()
}

fn key_sequence(key: &str) -> Option<Vec<String>> {
    let parts = key
        .split('+')
        .map(str::trim)
        .filter(|part| !part.is_empty())
        .collect::<Vec<_>>();
    let (key_part, modifier_parts) = parts.split_last()?;
    if modifier_parts.is_empty() {
        if let Some(modifier) = modifier_keycode(key_part) {
            return Some(vec![format!("{modifier}:1"), format!("{modifier}:0")]);
        }
    }
    let mut modifiers = Vec::new();
    for part in modifier_parts {
        modifiers.push(modifier_keycode(part)?);
    }
    let keycode = keycode(key_part)?;

    let mut events = Vec::new();
    for modifier in &modifiers {
        events.push(format!("{modifier}:1"));
    }
    events.push(format!("{keycode}:1"));
    events.push(format!("{keycode}:0"));
    for modifier in modifiers.iter().rev() {
        events.push(format!("{modifier}:0"));
    }
    Some(events)
}

fn modifier_keycode(key: &str) -> Option<u16> {
    match normalize_key(key).as_str() {
        "ctrl" | "control" => Some(29),
        "alt" | "option" => Some(56),
        "shift" => Some(42),
        "meta" | "super" | "cmd" | "command" => Some(125),
        _ => None,
    }
}

fn keycode(key: &str) -> Option<u16> {
    match normalize_key(key).as_str() {
        "enter" | "return" => Some(28),
        "escape" | "esc" => Some(1),
        "tab" => Some(15),
        "backspace" => Some(14),
        "delete" | "del" => Some(111),
        "space" => Some(57),
        "home" => Some(102),
        "end" => Some(107),
        "pageup" | "page_up" => Some(104),
        "pagedown" | "page_down" => Some(109),
        "arrowleft" | "left" => Some(105),
        "arrowright" | "right" => Some(106),
        "arrowup" | "up" => Some(103),
        "arrowdown" | "down" => Some(108),
        "f1" => Some(59),
        "f2" => Some(60),
        "f3" => Some(61),
        "f4" => Some(62),
        "f5" => Some(63),
        "f6" => Some(64),
        "f7" => Some(65),
        "f8" => Some(66),
        "f9" => Some(67),
        "f10" => Some(68),
        "f11" => Some(87),
        "f12" => Some(88),
        value if value.len() == 1 => keycode_for_ascii(value.as_bytes()[0] as char),
        _ => None,
    }
}

fn normalize_key(key: &str) -> String {
    key.trim().to_ascii_lowercase().replace(['-', ' '], "")
}

fn keycode_for_ascii(value: char) -> Option<u16> {
    match value {
        'a' => Some(30),
        'b' => Some(48),
        'c' => Some(46),
        'd' => Some(32),
        'e' => Some(18),
        'f' => Some(33),
        'g' => Some(34),
        'h' => Some(35),
        'i' => Some(23),
        'j' => Some(36),
        'k' => Some(37),
        'l' => Some(38),
        'm' => Some(50),
        'n' => Some(49),
        'o' => Some(24),
        'p' => Some(25),
        'q' => Some(16),
        'r' => Some(19),
        's' => Some(31),
        't' => Some(20),
        'u' => Some(22),
        'v' => Some(47),
        'w' => Some(17),
        'x' => Some(45),
        'y' => Some(21),
        'z' => Some(44),
        '1' => Some(2),
        '2' => Some(3),
        '3' => Some(4),
        '4' => Some(5),
        '5' => Some(6),
        '6' => Some(7),
        '7' => Some(8),
        '8' => Some(9),
        '9' => Some(10),
        '0' => Some(11),
        _ => None,
    }
}

fn user_id() -> Option<String> {
    let output = Command::new("id").arg("-u").output().ok()?;
    output
        .status
        .success()
        .then(|| String::from_utf8_lossy(&output.stdout).trim().to_string())
        .filter(|value| !value.is_empty())
}

fn list_process_apps() -> Vec<AppCandidate> {
    let output = Command::new("ps")
        .args(["-eo", "pid=,comm=,args="])
        .output();
    let Ok(output) = output else {
        return Vec::new();
    };
    if !output.status.success() {
        return Vec::new();
    }

    String::from_utf8_lossy(&output.stdout)
        .lines()
        .filter_map(parse_process_line)
        .filter(|app| looks_like_desktop_app(&app.name, &app.command))
        .take(50)
        .collect()
}

fn parse_process_line(line: &str) -> Option<AppCandidate> {
    let trimmed = line.trim();
    let mut parts = trimmed.splitn(3, char::is_whitespace);
    let pid = parts.next()?.parse().ok()?;
    let name = parts.next()?.to_string();
    let command = parts.next().unwrap_or("").trim().to_string();
    Some(AppCandidate { name, pid, command })
}

fn looks_like_desktop_app(name: &str, command: &str) -> bool {
    let haystack = format!("{name} {command}").to_ascii_lowercase();
    [
        "codex",
        "electron",
        "chrome",
        "chromium",
        "firefox",
        "brave",
        "code",
        "gnome-terminal",
        "ptyxis",
        "kgx",
        "nautilus",
        "slack",
        "discord",
        "spotify",
        "obsidian",
    ]
    .iter()
    .any(|needle| haystack.contains(needle))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::atspi_tree::{AccessibilityAction, Bounds};
    use crate::windows::{WindowBounds, GNOME_SHELL_EXTENSION_BACKEND};
    use std::io::{BufRead, Write};
    fn claim_context(owner: &str, token: &str) -> ClaimContext {
        ClaimContext {
            owner_thread_id: Some(owner.to_string()),
            claim_token: Some(token.to_string()),
        }
    }

    fn backend_with_live_claim(window_id: u64) -> (ComputerUseLinux, PathBuf) {
        let root = env::temp_dir().join(format!(
            "computer-use-linux-server-claim-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&root).unwrap();
        let binding = std::collections::BTreeMap::from([
            (
                "hyprland_instance".to_string(),
                serde_json::json!("instance"),
            ),
            ("uid".to_string(), serde_json::json!(1000)),
            (
                "wayland_display".to_string(),
                serde_json::json!("wayland-1"),
            ),
            (
                "xdg_runtime_dir".to_string(),
                serde_json::json!("/run/user/1000"),
            ),
        ]);
        let coordinator = Coordinator {
            state_dir: root.clone(),
            binding,
        };
        let session = coordinator.binding_key();
        let state = serde_json::json!({
            "version": 1,
            "sessions": {
                session: {
                    "binding": coordinator.binding,
                    "claims": {
                        "capture:test": {
                            "owner_thread_id": "owner-a",
                            "claim_token": "token-a",
                            "expires_at": f64::MAX,
                            "window": {"address": format!("0x{window_id:x}")}
                        }
                    }
                }
            }
        });
        std::fs::write(
            root.join("window-claims.json"),
            serde_json::to_vec(&state).unwrap(),
        )
        .unwrap();
        let backend = ComputerUseLinux::default();
        assert!(backend.claim_coordinator.set(Some(coordinator)).is_ok());
        (backend, root)
    }

    fn cache_observation(backend: &ComputerUseLinux, nodes: &[AccessibilityNode]) -> String {
        backend.record_accessibility_snapshot(AccessibilitySnapshotTarget::Desktop, nodes)
    }

    fn cache_window_observation(backend: &ComputerUseLinux, nodes: &[AccessibilityNode]) -> String {
        backend.record_accessibility_snapshot(
            AccessibilitySnapshotTarget::Window {
                window_id: 42,
                pid: Some(4242),
            },
            nodes,
        )
    }

    fn action(index: i32, name: &str, description: &str) -> AccessibilityAction {
        AccessibilityAction {
            index,
            name: name.to_string(),
            description: description.to_string(),
            keybinding: String::new(),
        }
    }

    #[test]
    fn exported_tool_schemas_omit_unsigned_integer_formats() {
        let tools = ComputerUseLinux::default().mcp_tool_router().list_all();
        let value = serde_json::to_value(tools).unwrap();
        let mut unsupported = Vec::new();
        collect_unsigned_integer_formats(&value, "$", &mut unsupported);

        assert!(
            unsupported.is_empty(),
            "unsupported unsigned integer formats: {unsupported:?}"
        );
    }

    #[test]
    fn get_app_state_schema_describes_metadata_without_embedded_image_data() {
        let tool = ComputerUseLinux::default()
            .mcp_tool_router()
            .list_all()
            .into_iter()
            .find(|tool| tool.name == "get_app_state")
            .unwrap();
        let schema = serde_json::to_string(&tool.output_schema).unwrap();
        let input_schema = serde_json::to_string(&tool.input_schema).unwrap();

        assert!(tool.output_schema.is_some());
        assert!(schema.contains("coordinate_width"));
        assert!(schema.contains("checkpoint_id"));
        assert!(schema.contains("observation_id"));
        assert!(schema.contains("screenshot_regions"));
        assert!(input_schema.contains("base_checkpoint_id"));
        assert!(input_schema.contains("observation_mode"));
        assert!(!schema.contains("data_url"));
    }

    #[test]
    fn action_batch_and_observe_schema_exposes_batch_and_adaptive_options() {
        let tool = ComputerUseLinux::default()
            .mcp_tool_router()
            .list_all()
            .into_iter()
            .find(|tool| tool.name == "run_action_batch_and_observe")
            .unwrap();
        let input_schema = serde_json::to_string(&tool.input_schema).unwrap();
        let output_schema = serde_json::to_string(&tool.output_schema).unwrap();

        assert!(tool.output_schema.is_some());
        assert!(input_schema.contains("window_id"));
        assert!(input_schema.contains("actions"));
        assert!(input_schema.contains("observation"));
        assert!(input_schema.contains("base_checkpoint_id"));
        assert!(output_schema.contains("batch"));
        assert!(output_schema.contains("observation"));
        assert!(output_schema.contains("observation_error"));
        assert!(output_schema.contains("accessibility_tree"));
    }

    #[test]
    fn post_action_observation_is_exact_window_scoped_and_adaptive() {
        let observation = PostActionObservationParams {
            base_checkpoint_id: Some("checkpoint-7".to_string()),
            checkpoint_interval: Some(5),
            force_checkpoint: Some(true),
            include_screenshot: Some(false),
            max_width: Some(800),
            max_height: Some(600),
            max_bytes: Some(200_000),
            scale: Some(0.75),
            format: Some(ScreenshotOutputFormat::Jpeg),
            quality: Some(70),
            max_nodes: Some(80),
            max_depth: Some(8),
        };

        assert_eq!(
            serde_json::to_value(
                observation.into_get_app_state_params(42, ClaimContext::default()),
            )
            .unwrap(),
            serde_json::json!({
                "app_name_or_bundle_identifier": null,
                "window_id": 42,
                "pid": null,
                "tty": null,
                "terminal_pid": null,
                "terminal_command": null,
                "terminal_cwd": null,
                "app_id": null,
                "wm_class": null,
                "title": null,
                "max_nodes": 80,
                "max_depth": 8,
                "include_screenshot": false,
                "observation_mode": "adaptive",
                "base_checkpoint_id": "checkpoint-7",
                "checkpoint_interval": 5,
                "force_checkpoint": true,
                "max_width": 800,
                "max_height": 600,
                "max_bytes": 200_000,
                "scale": 0.75,
                "format": "jpeg",
                "quality": 70,
                "verbose": false,
            })
        );
    }

    #[test]
    fn batch_and_observation_result_carries_observation_images_once() {
        let batch = ActionBatchOutput {
            ok: false,
            completed: 0,
            failed_at: Some(0),
            results: Vec::new(),
            error: Some("Action 0 failed.".to_string()),
        };
        let observation = serde_json::json!({"message": "post-action state"});
        let mut observation_result = CallToolResult::structured(observation.clone());
        observation_result
            .content
            .push(Content::image("AAAA", "image/png"));
        observation_result
            .content
            .push(Content::image("BBBB", "image/png"));

        let result = action_batch_and_observation_tool_result(
            batch.clone(),
            PostActionObservationResult::Completed(observation_result),
        )
        .unwrap();
        let expected = serde_json::json!({
            "batch": batch,
            "observation": observation,
            "observation_error": null,
        });
        let serialized = serde_json::to_string(&result).unwrap();

        assert_eq!(result.structured_content.as_ref(), Some(&expected));
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(
                &result.content[0].raw.as_text().unwrap().text
            )
            .unwrap(),
            expected
        );
        assert_eq!(result.content.len(), 3);
        assert_eq!(serialized.matches("AAAA").count(), 1);
        assert_eq!(serialized.matches("BBBB").count(), 1);
    }

    #[test]
    fn batch_and_observation_result_compacts_oversized_model_context() {
        let batch = ActionBatchOutput {
            ok: true,
            completed: 0,
            failed_at: None,
            results: Vec::new(),
            error: None,
        };
        let nodes = (0..128)
            .map(|index| serde_json::json!({"index": index, "name": "x".repeat(1_000)}))
            .collect::<Vec<_>>();
        let mut observation_result = CallToolResult::structured(serde_json::json!({
            "accessibility_tree": nodes,
            "message": "post-action state",
        }));
        observation_result
            .content
            .push(Content::image("AAAA", "image/png"));

        let result = action_batch_and_observation_tool_result(
            batch,
            PostActionObservationResult::Completed(observation_result),
        )
        .unwrap();
        let structured = result.structured_content.as_ref().unwrap();
        let retained_nodes = structured["observation"]["accessibility_tree"]
            .as_array()
            .unwrap();

        assert!(serde_json::to_vec(structured).unwrap().len() <= 8 * 1024);
        assert!(!retained_nodes.is_empty());
        assert!(retained_nodes.len() < 128);
        assert!(retained_nodes
            .iter()
            .all(|node| node["name"].as_str().unwrap().len() <= 512));
        assert!(structured["observation_error"]
            .as_str()
            .unwrap()
            .contains("truncated"));
        assert_eq!(result.content.len(), 2);
    }

    #[test]
    fn validation_failure_result_has_no_observation_content() {
        let batch = ActionBatchOutput::validation_error("invalid batch".to_string());
        let result = action_batch_and_observation_tool_result(
            batch.clone(),
            PostActionObservationResult::NotAttempted,
        )
        .unwrap();

        assert_eq!(
            result.structured_content,
            Some(serde_json::json!({
                "batch": batch,
                "observation": null,
                "observation_error": null,
            }))
        );
        assert_eq!(result.content.len(), 1);
    }

    #[test]
    fn observation_failure_preserves_batch_and_bounds_the_error() {
        let batch = ActionBatchOutput {
            ok: false,
            completed: 0,
            failed_at: Some(0),
            results: Vec::new(),
            error: Some("Action 0 failed.".to_string()),
        };
        let observation_error =
            ErrorData::internal_error(format!("post-action failure\n{}", "x".repeat(600)), None);
        let result = action_batch_and_observation_tool_result(
            batch.clone(),
            PostActionObservationResult::Failed(observation_error),
        )
        .unwrap();
        let structured = result.structured_content.unwrap();
        let error = structured["observation_error"].as_str().unwrap();

        assert_eq!(structured["batch"], serde_json::to_value(batch).unwrap());
        assert_eq!(structured["observation"], serde_json::Value::Null);
        assert!(error.len() <= 512);
        assert!(error.ends_with("... [truncated]"));
        assert!(!error.contains('\n'));
        assert_eq!(result.content.len(), 1);
    }

    #[test]
    fn click_scroll_and_semantic_action_schemas_handle_observation_ids() {
        let tools = ComputerUseLinux::default().mcp_tool_router().list_all();
        for name in ["click", "scroll", "run_action_batch"] {
            let tool = tools.iter().find(|tool| tool.name == name).unwrap();
            assert!(serde_json::to_string(&tool.input_schema)
                .unwrap()
                .contains("observation_id"));
        }
        for name in ["perform_action", "set_value"] {
            let tool = tools.iter().find(|tool| tool.name == name).unwrap();
            let schema = serde_json::to_value(&tool.input_schema).unwrap();
            assert!(schema["required"]
                .as_array()
                .is_some_and(|required| required.iter().any(|field| field == "observation_id")));
        }
    }

    #[test]
    fn window_scoped_tools_expose_shared_claim_credentials() {
        let tools = ComputerUseLinux::default().mcp_tool_router().list_all();
        for name in [
            "activate_window",
            "get_app_state",
            "screenshot",
            "perform_action",
            "set_value",
            "click",
            "scroll",
            "drag",
            "press_key",
            "type_text",
            "run_action_batch",
            "run_action_batch_and_observe",
        ] {
            let tool = tools.iter().find(|tool| tool.name == name).unwrap();
            let schema = serde_json::to_value(&tool.input_schema).unwrap();
            assert!(
                schema["properties"].get("owner_thread_id").is_some(),
                "{name}"
            );
            assert!(schema["properties"].get("claim_token").is_some(), "{name}");
        }
    }

    #[test]
    fn exact_window_pin_preserves_selectors_and_authorizes_one_identity() {
        let mut target = WindowTarget {
            app_id: Some("org.example.App".to_string()),
            title: Some("Document".to_string()),
            ..Default::default()
        };

        target.pin_exact_window_id(42);

        assert_eq!(
            target,
            WindowTarget {
                window_id: Some(42),
                app_id: Some("org.example.App".to_string()),
                title: Some("Document".to_string()),
                ..Default::default()
            }
        );
    }

    #[tokio::test]
    async fn screenshot_route_rejects_foreign_claim_credentials() {
        let window_id = u64::MAX;
        let (backend, root) = backend_with_live_claim(window_id);
        let error = backend
            .screenshot(Parameters(ScreenshotParams {
                window_id: Some(window_id),
                claim: ClaimContext {
                    owner_thread_id: Some("owner-b".to_string()),
                    claim_token: Some("token-b".to_string()),
                },
                ..Default::default()
            }))
            .await
            .unwrap_err();
        assert!(error.message.contains("actively claimed"));
        std::fs::remove_dir_all(root).unwrap();
    }

    #[tokio::test]
    async fn screenshot_route_accepts_matching_claim_credentials() {
        let window_id = u64::MAX;
        let (backend, root) = backend_with_live_claim(window_id);
        let error = backend
            .screenshot(Parameters(ScreenshotParams {
                window_id: Some(window_id),
                claim: ClaimContext {
                    owner_thread_id: Some("owner-a".to_string()),
                    claim_token: Some("token-a".to_string()),
                },
                ..Default::default()
            }))
            .await
            .unwrap_err();
        assert!(!error.message.contains("claim_coordination"));
        assert!(!error.message.contains("actively claimed"));
        std::fs::remove_dir_all(root).unwrap();
    }

    #[tokio::test]
    async fn screenshot_focus_waits_for_the_companion_global_lane() {
        let window_id = u64::MAX;
        let (backend, root) = backend_with_live_claim(window_id);
        let lock_path = root.join("pointer-transaction.lock");
        let mut companion = std::process::Command::new("python3")
            .args([
                "-c",
                "import fcntl,sys; f=open(sys.argv[1], 'a+'); fcntl.flock(f, fcntl.LOCK_EX); print('locked', flush=True); input()",
            ])
            .arg(lock_path)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .unwrap();
        let mut ready = String::new();
        std::io::BufReader::new(companion.stdout.take().unwrap())
            .read_line(&mut ready)
            .unwrap();
        assert_eq!(ready, "locked\n");

        let mut capture = tokio::spawn(async move {
            backend
                .screenshot(Parameters(ScreenshotParams {
                    window_id: Some(window_id),
                    claim: ClaimContext {
                        owner_thread_id: Some("owner-a".to_string()),
                        claim_token: Some("token-a".to_string()),
                    },
                    ..Default::default()
                }))
                .await
        });
        assert!(
            tokio::time::timeout(Duration::from_millis(50), &mut capture)
                .await
                .is_err()
        );
        companion.stdin.take().unwrap().write_all(b"\n").unwrap();
        assert!(companion.wait().unwrap().success());

        let error = tokio::time::timeout(Duration::from_secs(5), capture)
            .await
            .unwrap()
            .unwrap()
            .unwrap_err();
        assert!(!error.message.contains("claim_coordination"));
        assert!(!error.message.contains("actively claimed"));
        std::fs::remove_dir_all(root).unwrap();
    }

    #[tokio::test]
    async fn get_app_state_route_rejects_desktop_capture_during_a_live_claim() {
        let (backend, root) = backend_with_live_claim(u64::MAX);
        let params = serde_json::from_value(serde_json::json!({})).unwrap();
        let error = backend.get_app_state(Parameters(params)).await.unwrap_err();
        assert!(error.message.contains("window_id is required"));
        std::fs::remove_dir_all(root).unwrap();
    }

    #[tokio::test]
    async fn mutation_routes_reject_missing_and_wrong_claims_but_accept_matching_claims() {
        let (backend, root) = backend_with_live_claim(42);
        let observation_id = cache_window_observation(
            &backend,
            &[node_with_actions(7, None, vec![click_action()])],
        );
        for (claim, blocked) in [
            (ClaimContext::default(), true),
            (claim_context("owner-b", "token-b"), true),
            (claim_context("owner-a", "token-a"), false),
        ] {
            let Json(activate) = backend
                .activate_window(Parameters(ActivateWindowParams {
                    claim: claim.clone(),
                    window_id: Some(42),
                    ..Default::default()
                }))
                .await;
            let Json(semantic) = backend
                .perform_action(Parameters(ActionParams {
                    claim: claim.clone(),
                    observation_id: observation_id.clone(),
                    element_index: Some(7),
                    ..Default::default()
                }))
                .await;
            let Json(value) = backend
                .set_value(Parameters(SetValueParams {
                    claim: claim.clone(),
                    observation_id: observation_id.clone(),
                    element_index: Some(7),
                    value: "next".to_string(),
                    ..Default::default()
                }))
                .await;
            let Json(text) = backend
                .type_text(Parameters(TypeTextParams::for_window(
                    42,
                    "hello".to_string(),
                    claim.clone(),
                )))
                .await;
            let Json(key) = backend
                .press_key(Parameters(PressKeyParams::for_window(
                    42,
                    "Tab".to_string(),
                    claim.clone(),
                )))
                .await;
            let Json(click) = backend
                .click(Parameters(ClickParams {
                    claim: claim.clone(),
                    x: Some(1),
                    y: Some(1),
                    window_id: Some(42),
                    ..Default::default()
                }))
                .await;
            let Json(scroll) = backend
                .scroll(Parameters(ScrollParams {
                    claim: claim.clone(),
                    x: Some(1),
                    y: Some(1),
                    direction: "down".to_string(),
                    window_id: Some(42),
                    ..Default::default()
                }))
                .await;
            let Json(drag) = backend
                .drag(Parameters(DragParams {
                    claim: claim.clone(),
                    start_x: 1,
                    start_y: 1,
                    end_x: 2,
                    end_y: 2,
                    window_id: Some(42),
                    ..Default::default()
                }))
                .await;
            let batch = backend
                .run_action_batch_and_observe(Parameters(ActionBatchAndObserveParams {
                    batch: ActionBatchParams {
                        claim,
                        window_id: 42,
                        actions: vec![BatchAction::PressKey {
                            key: "Tab".to_string(),
                        }],
                    },
                    observation: PostActionObservationParams {
                        include_screenshot: Some(false),
                        ..Default::default()
                    },
                }))
                .await
                .map_or_else(
                    |error| error.message.to_string(),
                    |result| format!("{result:?}"),
                );

            for output in [
                serde_json::to_string(&activate).unwrap(),
                serde_json::to_string(&semantic).unwrap(),
                serde_json::to_string(&value).unwrap(),
                serde_json::to_string(&text).unwrap(),
                serde_json::to_string(&key).unwrap(),
                serde_json::to_string(&click).unwrap(),
                serde_json::to_string(&scroll).unwrap(),
                serde_json::to_string(&drag).unwrap(),
                batch,
            ] {
                assert_eq!(output.contains("actively claimed"), blocked, "{output}");
                assert!(!output.contains("token-a"));
                assert!(!output.contains("token-b"));
            }
        }
        std::fs::remove_dir_all(root).unwrap();
    }

    #[tokio::test]
    async fn activate_window_waits_for_the_companion_global_lane() {
        let (backend, root) = backend_with_live_claim(42);
        let lock_path = root.join("pointer-transaction.lock");
        let mut companion = std::process::Command::new("python3")
            .args([
                "-c",
                "import fcntl,sys; f=open(sys.argv[1], 'a+'); fcntl.flock(f, fcntl.LOCK_EX); print('locked', flush=True); input()",
            ])
            .arg(lock_path)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .unwrap();
        let mut ready = String::new();
        std::io::BufReader::new(companion.stdout.take().unwrap())
            .read_line(&mut ready)
            .unwrap();
        assert_eq!(ready, "locked\n");

        let mut action = tokio::spawn(async move {
            backend
                .activate_window(Parameters(ActivateWindowParams {
                    claim: claim_context("owner-a", "token-a"),
                    window_id: Some(42),
                    ..Default::default()
                }))
                .await
        });
        assert!(tokio::time::timeout(Duration::from_millis(50), &mut action)
            .await
            .is_err());
        companion.stdin.take().unwrap().write_all(b"\n").unwrap();
        assert!(companion.wait().unwrap().success());

        let Json(output) = tokio::time::timeout(Duration::from_secs(5), action)
            .await
            .unwrap()
            .unwrap();
        let output = serde_json::to_string(&output).unwrap();
        assert!(!output.contains("actively claimed"));
        assert!(!output.contains("token-a"));
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn action_batch_preflight_rejects_unsupported_keys_before_execution() {
        let params = ActionBatchParams {
            claim: ClaimContext::default(),
            window_id: 42,
            actions: vec![
                BatchAction::PressKey {
                    key: "Tab".to_string(),
                },
                BatchAction::PressKey {
                    key: "DefinitelyNotAKey".to_string(),
                },
            ],
        };

        assert_eq!(
            ComputerUseLinux::default().validate_action_batch(&params),
            Err("actions[1] has an unsupported key. Use names like Enter, Escape, Tab, ArrowLeft, Ctrl+L, or a single US keyboard letter/digit.".to_string())
        );
    }

    #[test]
    fn action_batch_preflight_rejects_mismatched_click_observation() {
        let backend = ComputerUseLinux::default();
        let observation_id = cache_window_observation(
            &backend,
            &[node(
                7,
                Some(Bounds {
                    x: 10,
                    y: 20,
                    width: 100,
                    height: 40,
                }),
            )],
        );
        let params = ActionBatchParams {
            claim: ClaimContext::default(),
            window_id: 41,
            actions: vec![BatchAction::Click(BatchClick {
                observation_id: Some(observation_id),
                element_index: Some(7),
                ..Default::default()
            })],
        };

        assert!(backend
            .validate_action_batch(&params)
            .unwrap_err()
            .contains("does not match the requested target window"));
    }

    #[test]
    fn app_state_result_carries_each_image_once_and_only_metadata_in_structured_content() {
        let capture = ScreenshotCapture {
            mime_type: "image/png".to_string(),
            data_url: "data:image/png;base64,AAAA".to_string(),
            source: "test".to_string(),
            width: 2,
            height: 1,
            coordinate_width: 4,
            coordinate_height: 2,
            scale: 0.5,
            resized: true,
            bytes: 3,
            original_bytes: 6,
            max_bytes: 1024,
            format: ScreenshotOutputFormat::Png,
            quality: None,
        };
        let diagnostics = doctor_report();
        let output = GetAppStateOutput {
            app_name_or_bundle_identifier: Some("example.app".to_string()),
            window_context: None,
            window_error: None,
            window_permissions_hint: None,
            backend: "linux-atspi".to_string(),
            screenshot: Some(ScreenshotMetadata::from_capture(&capture, None, (0, 0))),
            screenshot_regions: Vec::new(),
            screenshot_error: None,
            accessibility_tree: Vec::new(),
            accessibility_tree_raw_count: 0,
            observation_id: None,
            accessibility_coordinate_space: "desktop".to_string(),
            accessibility_error: None,
            readiness: diagnostics.readiness,
            diagnostics: None,
            observation: None,
            message: "ready".to_string(),
        };

        let mut second_capture = capture.clone();
        second_capture.data_url = "data:image/png;base64,BBBB".to_string();
        let result = app_state_tool_result(output, &[&capture, &second_capture]).unwrap();
        let structured = result.structured_content.as_ref().unwrap();
        let text: serde_json::Value = serde_json::from_str(
            result.content[0]
                .raw
                .as_text()
                .expect("first content block should be text")
                .text
                .as_str(),
        )
        .unwrap();
        let serialized = serde_json::to_string(&result).unwrap();

        assert_eq!(&text, structured);
        assert_eq!(structured["screenshot"]["coordinate_width"], 4);
        assert_eq!(structured["screenshot"]["coordinate_space"], "desktop");
        assert_eq!(structured["screenshot"]["coordinate_origin_x"], 0);
        assert_eq!(structured["screenshot"]["coordinate_origin_y"], 0);
        assert!(structured["screenshot"].get("data_url").is_none());
        assert_eq!(serialized.matches("AAAA").count(), 1);
        assert_eq!(serialized.matches("BBBB").count(), 1);
        assert_eq!(result.content.len(), 3);
    }

    #[test]
    fn failed_app_state_screenshot_cannot_leak_image_content() {
        let capture = ScreenshotCapture {
            mime_type: "image/png".to_string(),
            data_url: "data:image/png;base64,DESKTOP_PIXELS".to_string(),
            source: "test".to_string(),
            width: 2,
            height: 1,
            coordinate_width: 2,
            coordinate_height: 1,
            scale: 1.0,
            resized: false,
            bytes: 14,
            original_bytes: 14,
            max_bytes: 1024,
            format: ScreenshotOutputFormat::Png,
            quality: None,
        };
        let diagnostics = doctor_report();
        let output = GetAppStateOutput {
            app_name_or_bundle_identifier: None,
            window_context: None,
            window_error: None,
            window_permissions_hint: None,
            backend: "linux-atspi".to_string(),
            screenshot: Some(ScreenshotMetadata::from_capture(&capture, None, (0, 0))),
            screenshot_regions: vec![ScreenshotRegionMetadata::from_capture(
                ObservationRegion {
                    x: 0,
                    y: 0,
                    width: 2,
                    height: 1,
                },
                &capture,
                None,
                (0, 0),
            )],
            screenshot_error: Some(
                "targeted exact window capture failed; refusing to capture the full desktop"
                    .to_string(),
            ),
            accessibility_tree: Vec::new(),
            accessibility_tree_raw_count: 0,
            observation_id: None,
            accessibility_coordinate_space: "desktop".to_string(),
            accessibility_error: None,
            readiness: diagnostics.readiness,
            diagnostics: None,
            observation: None,
            message: "Targeted screenshot failed.".to_string(),
        };

        let result = app_state_tool_result(output, &[&capture]).unwrap();
        let serialized = serde_json::to_string(&result).unwrap();

        assert_eq!(result.content.len(), 1);
        assert_eq!(
            result.structured_content.as_ref().unwrap()["screenshot"],
            serde_json::Value::Null
        );
        assert_eq!(
            result.structured_content.as_ref().unwrap()["screenshot_regions"],
            serde_json::json!([])
        );
        assert!(!serialized.contains("DESKTOP_PIXELS"));
    }

    #[test]
    fn targeted_screenshot_errors_include_fail_closed_metadata() {
        let target = WindowTarget {
            app_id: Some("org.example.Editor".to_string()),
            ..Default::default()
        };

        let error = screenshot_failure(
            "window_resolution",
            Some(&target),
            "app_id matched multiple windows",
        );

        assert_eq!(error.code, rmcp::model::ErrorCode::INTERNAL_ERROR);
        assert_eq!(
            error.data,
            Some(serde_json::json!({
                "stage": "window_resolution",
                "target": {
                    "window_id": null,
                    "pid": null,
                    "terminal_pid": null,
                    "selector_fields": ["app_id"],
                },
                "image_returned": false,
            }))
        );
    }

    #[test]
    fn targeted_screenshot_errors_bound_caller_controlled_details() {
        let unbounded = "x".repeat(20_000);
        let target = WindowTarget {
            title: Some(unbounded.clone()),
            ..Default::default()
        };

        let error = screenshot_failure("window_resolution", Some(&target), &unbounded);
        let serialized = serde_json::to_string(&error).unwrap();

        assert!(serialized.len() < 2_000);
        assert!(!serialized.contains(&unbounded));
    }

    #[test]
    fn changed_region_metadata_uses_window_local_image_origin() {
        let capture = ScreenshotCapture {
            mime_type: "image/png".to_string(),
            data_url: "data:image/png;base64,AAAA".to_string(),
            source: "test".to_string(),
            width: 20,
            height: 10,
            coordinate_width: 20,
            coordinate_height: 10,
            scale: 1.0,
            resized: false,
            bytes: 3,
            original_bytes: 3,
            max_bytes: 1024,
            format: ScreenshotOutputFormat::Png,
            quality: None,
        };
        let window = WindowInfo {
            window_id: 42,
            title: None,
            app_id: None,
            wm_class: None,
            pid: None,
            bounds: None,
            workspace: None,
            focused: false,
            hidden: false,
            client_type: None,
            backend: "test".to_string(),
            terminal: None,
        };
        let metadata = ScreenshotRegionMetadata::from_capture(
            ObservationRegion {
                x: 30,
                y: 40,
                width: 20,
                height: 10,
            },
            &capture,
            Some(&window),
            (5, 7),
        );

        assert_eq!(
            (
                metadata.screenshot.coordinate_space.as_str(),
                metadata.screenshot.coordinate_origin_x,
                metadata.screenshot.coordinate_origin_y,
                metadata.screenshot.window_id,
            ),
            ("window_local", 35, 47, Some(42)),
        );
    }

    #[tokio::test]
    async fn app_state_observation_modes_respect_screenshot_opt_out() {
        for observation_mode in [ObservationMode::Adaptive, ObservationMode::Full] {
            let params = serde_json::from_value(serde_json::json!({
                "include_screenshot": false,
                "observation_mode": observation_mode,
            }))
            .unwrap();
            let result = ComputerUseLinux::default()
                .get_app_state(Parameters(params))
                .await
                .unwrap();

            assert_eq!(result.content.len(), 1);
            assert_eq!(
                result.structured_content.unwrap()["screenshot"],
                serde_json::Value::Null
            );
        }
    }

    #[test]
    fn diagnostics_cache_reuses_and_explicitly_invalidates_report() {
        let server = ComputerUseLinux::default();
        server.diagnostics();
        let captured_at = server
            .diagnostics_cache
            .lock()
            .unwrap()
            .report
            .as_ref()
            .unwrap()
            .0;

        server.diagnostics();
        assert_eq!(
            server
                .diagnostics_cache
                .lock()
                .unwrap()
                .report
                .as_ref()
                .unwrap()
                .0,
            captured_at
        );

        server.invalidate_diagnostics();
        let cache = server.diagnostics_cache.lock().unwrap();
        assert_eq!(cache.generation, 1);
        assert!(cache.report.is_none());
    }

    fn collect_unsigned_integer_formats(
        value: &serde_json::Value,
        path: &str,
        unsupported: &mut Vec<String>,
    ) {
        match value {
            serde_json::Value::Object(object) => {
                if matches!(
                    object.get("format").and_then(serde_json::Value::as_str),
                    Some("uint" | "uint8" | "uint16" | "uint32" | "uint64" | "usize")
                ) {
                    unsupported.push(path.to_string());
                }
                for (key, nested) in object {
                    collect_unsigned_integer_formats(nested, &format!("{path}/{key}"), unsupported);
                }
            }
            serde_json::Value::Array(items) => {
                for (index, nested) in items.iter().enumerate() {
                    collect_unsigned_integer_formats(
                        nested,
                        &format!("{path}/{index}"),
                        unsupported,
                    );
                }
            }
            _ => {}
        }
    }

    fn node(index: u32, bounds: Option<Bounds>) -> AccessibilityNode {
        node_with_actions(index, bounds, Vec::new())
    }

    fn node_with_actions(
        index: u32,
        bounds: Option<Bounds>,
        actions: Vec<AccessibilityAction>,
    ) -> AccessibilityNode {
        AccessibilityNode {
            index,
            parent_index: None,
            depth: 0,
            object_ref: format!(":1.{index}/org/a11y/atspi/accessible/{index}"),
            role: "push button".to_string(),
            name: Some(format!("Button {index}")),
            description: None,
            child_count: 0,
            bounds,
            states: Vec::new(),
            actions,
            value: None,
            text: None,
            supports_editable_text: false,
        }
    }

    fn click_action() -> AccessibilityAction {
        AccessibilityAction {
            index: 0,
            name: "Click".to_string(),
            description: "Clicks the element".to_string(),
            keybinding: String::new(),
        }
    }

    fn solid_png(width: u32, height: u32) -> Vec<u8> {
        let img = image::RgbaImage::from_pixel(width, height, image::Rgba([32, 128, 192, 255]));
        let mut out = Vec::new();
        image::DynamicImage::ImageRgba8(img)
            .write_to(&mut std::io::Cursor::new(&mut out), image::ImageFormat::Png)
            .unwrap();
        out
    }

    #[test]
    fn targeted_app_state_crops_before_screenshot_payload_resize() {
        let raw = RawScreenshotCapture {
            mime_type: "image/png".to_string(),
            bytes: solid_png(400, 200),
            source: "test".to_string(),
            width: 400,
            height: 200,
        };
        let bounds = WindowBounds {
            x: Some(50),
            y: Some(20),
            width: 200,
            height: 100,
        };
        let (raw, origin) = crop_raw_screenshot(raw, Some(&bounds), true).unwrap();
        let capture = prepare_screenshot_payload(
            raw,
            ScreenshotPayloadOptions {
                max_width: Some(100),
                max_height: Some(100),
                max_bytes: Some(1024 * 1024),
                ..Default::default()
            },
        )
        .unwrap();

        assert_eq!(origin, Some((0, 0)));
        assert_eq!(
            (capture.coordinate_width, capture.coordinate_height),
            (200, 100)
        );
        assert_eq!((capture.width, capture.height), (100, 50));
    }

    #[test]
    fn unresolved_app_state_target_refuses_full_desktop_screenshot() {
        let raw = RawScreenshotCapture {
            mime_type: "image/png".to_string(),
            bytes: solid_png(400, 200),
            source: "test".to_string(),
            width: 400,
            height: 200,
        };

        let error = crop_raw_screenshot(raw, None, true).unwrap_err();

        assert!(error.to_string().contains("requires a resolved window"));
    }

    #[test]
    fn targeted_screenshot_requires_bounds_before_desktop_capture() {
        let mut window = window_info(
            42,
            Some("Editor"),
            Some("org.example.Editor"),
            Some("Editor"),
            Some(1234),
        );
        window.bounds = None;

        let error = validated_target_bounds(Some(&window)).unwrap_err();

        assert!(error
            .to_string()
            .contains("requires resolved window bounds"));
        assert!(error
            .to_string()
            .contains("refusing to capture the full desktop"));
    }

    #[test]
    fn targeted_screenshot_rejects_unusable_bounds_before_desktop_capture() {
        let mut window = window_info(
            42,
            Some("Editor"),
            Some("org.example.Editor"),
            Some("Editor"),
            Some(1234),
        );
        window.bounds = Some(WindowBounds {
            x: Some(10),
            y: Some(20),
            width: 0,
            height: 100,
        });

        let error = validated_target_bounds(Some(&window)).unwrap_err();

        assert!(error.to_string().contains("unusable window bounds"));
        assert!(error
            .to_string()
            .contains("refusing to capture the full desktop"));
    }

    #[test]
    fn targeted_crop_failure_never_returns_the_source_capture() {
        let raw = RawScreenshotCapture {
            mime_type: "image/png".to_string(),
            bytes: b"not a png".to_vec(),
            source: "desktop-test".to_string(),
            width: 400,
            height: 200,
        };
        let bounds = WindowBounds {
            x: Some(50),
            y: Some(20),
            width: 200,
            height: 100,
        };

        let error = crop_raw_screenshot(raw, Some(&bounds), true).unwrap_err();

        assert!(error
            .to_string()
            .contains("targeted screenshot crop failed"));
    }

    #[test]
    fn targeted_app_state_crops_only_visible_part_of_offscreen_window() {
        let raw = RawScreenshotCapture {
            mime_type: "image/png".to_string(),
            bytes: solid_png(400, 200),
            source: "test".to_string(),
            width: 400,
            height: 200,
        };
        let bounds = WindowBounds {
            x: Some(-50),
            y: Some(-40),
            width: 100,
            height: 100,
        };

        let (raw, origin) = crop_raw_screenshot(raw, Some(&bounds), true).unwrap();
        let capture = prepare_screenshot_payload(raw, ScreenshotPayloadOptions::default()).unwrap();

        assert_eq!(origin, Some((50, 40)));
        assert_eq!(
            (capture.coordinate_width, capture.coordinate_height),
            (50, 60)
        );
    }

    #[test]
    fn wayland_display_is_enough_to_select_portal_fallback() {
        assert!(session_is_wayland(None, Some("wayland-1")));
        assert!(session_is_wayland(Some("wayland"), None));
        assert!(!session_is_wayland(Some("x11"), None));
    }

    #[test]
    fn window_crop_happens_before_screenshot_payload_resize() {
        let (cropped, width, height) = crop_png(&solid_png(400, 200), 50, 20, 200, 100).unwrap();
        let capture = prepare_screenshot_payload(
            RawScreenshotCapture {
                mime_type: "image/png".to_string(),
                bytes: cropped,
                source: "test".to_string(),
                width,
                height,
            },
            ScreenshotPayloadOptions {
                max_width: Some(100),
                max_height: Some(100),
                max_bytes: Some(1024 * 1024),
                ..Default::default()
            },
        )
        .unwrap();

        assert_eq!(
            (capture.coordinate_width, capture.coordinate_height),
            (200, 100)
        );
        assert_eq!((capture.width, capture.height), (100, 50));
        assert!(capture.resized);
    }

    fn window_info(
        window_id: u64,
        title: Option<&str>,
        app_id: Option<&str>,
        wm_class: Option<&str>,
        pid: Option<u32>,
    ) -> WindowInfo {
        WindowInfo {
            window_id,
            title: title.map(str::to_string),
            app_id: app_id.map(str::to_string),
            wm_class: wm_class.map(str::to_string),
            pid,
            bounds: Some(WindowBounds {
                x: Some(10),
                y: Some(20),
                width: 800,
                height: 600,
            }),
            workspace: Some(0),
            focused: false,
            hidden: false,
            client_type: Some("wayland".to_string()),
            backend: GNOME_SHELL_EXTENSION_BACKEND.to_string(),
            terminal: None,
        }
    }

    fn focus_result_with_bounds(bounds: Option<WindowBounds>) -> WindowFocusResult {
        let mut requested_window = window_info(
            42,
            Some("Target"),
            Some("target-app"),
            Some("target-app"),
            Some(4242),
        );
        requested_window.bounds = bounds;
        let mut focused_window = requested_window.clone();
        focused_window.focused = true;
        WindowFocusResult {
            requested_window,
            focused_window: Some(focused_window),
            exact_window_focused: true,
            app_focused: true,
            backend: GNOME_SHELL_EXTENSION_BACKEND.to_string(),
            note: "test focus".to_string(),
        }
    }

    fn window_bounds(x: Option<i32>, y: Option<i32>, width: u32, height: u32) -> WindowBounds {
        WindowBounds {
            x,
            y,
            width,
            height,
        }
    }

    #[test]
    fn relative_click_coordinates_use_verified_window_bounds() {
        let focus = focus_result_with_bounds(Some(window_bounds(Some(100), Some(200), 800, 600)));
        let mut params = ClickParams {
            x: Some(7),
            y: Some(9),
            relative: Some(true),
            ..Default::default()
        };

        apply_window_relative_click_coordinates(&mut params, &focus).unwrap();

        assert_eq!((params.x, params.y), (Some(107), Some(209)));
        for action in ["click", "scroll", "drag"] {
            let validate = |claim: &ClaimContext, point| {
                validate_claimed_window_point(claim, Some(&focus), point, action)
            };
            assert!(validate(&claim_context("owner-a", "token-a"), (107, 209)).is_ok());
            assert!(validate(&claim_context("owner-a", "token-a"), (99, 209)).is_err());
            assert!(validate(&ClaimContext::default(), (99, 209)).is_ok());
        }
    }

    #[test]
    fn relative_click_coordinates_prefer_focused_window_bounds() {
        let mut focus =
            focus_result_with_bounds(Some(window_bounds(Some(100), Some(200), 800, 600)));
        let focused_window = focus
            .focused_window
            .as_mut()
            .expect("test focus should include focused window");
        focused_window.bounds = Some(window_bounds(Some(300), Some(400), 800, 600));
        let mut params = ClickParams {
            x: Some(7),
            y: Some(9),
            relative: Some(true),
            ..Default::default()
        };

        apply_window_relative_click_coordinates(&mut params, &focus).unwrap();

        assert_eq!((params.x, params.y), (Some(307), Some(409)));
    }

    #[test]
    fn relative_click_coordinates_require_window_bounds_origin() {
        let focus = focus_result_with_bounds(Some(window_bounds(None, Some(200), 800, 600)));
        let mut params = ClickParams {
            x: Some(7),
            y: Some(9),
            relative: Some(true),
            ..Default::default()
        };

        let error = apply_window_relative_click_coordinates(&mut params, &focus).unwrap_err();

        assert!(error.contains("bounds with an origin"));
        assert_eq!((params.x, params.y), (Some(7), Some(9)));
    }

    #[test]
    fn relative_click_coordinates_require_xy() {
        let focus = focus_result_with_bounds(Some(window_bounds(Some(100), Some(200), 800, 600)));
        let mut params = ClickParams {
            x: Some(7),
            relative: Some(true),
            ..Default::default()
        };

        let error = apply_window_relative_click_coordinates(&mut params, &focus).unwrap_err();

        assert!(error.contains("both x and y"));
        assert_eq!((params.x, params.y), (Some(7), None));
    }

    #[test]
    fn relative_click_coordinates_must_stay_inside_bounds() {
        let focus = focus_result_with_bounds(Some(window_bounds(Some(100), Some(200), 800, 600)));

        for (x, y) in [(-1, 9), (7, -1), (800, 9), (7, 600)] {
            let mut params = ClickParams {
                x: Some(x),
                y: Some(y),
                relative: Some(true),
                ..Default::default()
            };

            let error = apply_window_relative_click_coordinates(&mut params, &focus).unwrap_err();

            assert!(error.contains("inside target-window bounds"));
            assert_eq!((params.x, params.y), (Some(x), Some(y)));
        }
    }

    #[test]
    fn accessibility_filter_candidates_prefer_title_and_skip_synthetic_app_id() {
        let window = window_info(
            42,
            Some("CU ATSPI GTK Test"),
            Some("window:46"),
            Some("cu_atspi_gtk_test.py"),
            Some(2914326),
        );

        let candidates = accessibility_filter_candidates(Some(&window));

        assert_eq!(
            candidates,
            vec![
                "CU ATSPI GTK Test".to_string(),
                "cu_atspi_gtk_test.py".to_string(),
            ]
        );
    }

    #[test]
    fn select_accessibility_object_ref_prefers_exact_pid_match() {
        let apps = vec![
            AccessibleAppSummary {
                object_ref: ":1.31/org/a11y/atspi/accessible/root".to_string(),
                name: Some("electron".to_string()),
                pid: Some(2774076),
                role: "application".to_string(),
                child_count: 1,
                bounds: None,
            },
            AccessibleAppSummary {
                object_ref: ":1.64/org/a11y/atspi/accessible/root".to_string(),
                name: Some("cu_atspi_gtk_test.py".to_string()),
                pid: Some(2914326),
                role: "application".to_string(),
                child_count: 1,
                bounds: None,
            },
        ];

        let object_ref = select_accessibility_object_ref(
            &apps,
            2914326,
            &[
                "CU ATSPI GTK Test".to_string(),
                "cu_atspi_gtk_test.py".to_string(),
            ],
        )
        .unwrap();

        assert_eq!(object_ref, ":1.64/org/a11y/atspi/accessible/root");
    }

    #[test]
    fn compact_accessibility_tree_reparents_actionable_descendants() {
        let nodes = vec![
            AccessibilityNode {
                index: 0,
                parent_index: None,
                depth: 0,
                object_ref: ":1.0/root".to_string(),
                role: "application".to_string(),
                name: Some("demo-app".to_string()),
                description: None,
                child_count: 1,
                bounds: None,
                states: Vec::new(),
                actions: Vec::new(),
                value: None,
                text: None,
                supports_editable_text: false,
            },
            AccessibilityNode {
                index: 1,
                parent_index: Some(0),
                depth: 1,
                object_ref: ":1.1/frame".to_string(),
                role: "frame".to_string(),
                name: Some("Demo Frame".to_string()),
                description: None,
                child_count: 1,
                bounds: None,
                states: Vec::new(),
                actions: Vec::new(),
                value: None,
                text: None,
                supports_editable_text: false,
            },
            AccessibilityNode {
                index: 2,
                parent_index: Some(1),
                depth: 2,
                object_ref: ":1.2/filler".to_string(),
                role: "filler".to_string(),
                name: None,
                description: None,
                child_count: 1,
                bounds: None,
                states: Vec::new(),
                actions: Vec::new(),
                value: None,
                text: None,
                supports_editable_text: false,
            },
            AccessibilityNode {
                index: 3,
                parent_index: Some(2),
                depth: 3,
                object_ref: ":1.3/button".to_string(),
                role: "button".to_string(),
                name: Some("Run".to_string()),
                description: None,
                child_count: 0,
                bounds: Some(Bounds {
                    x: 10,
                    y: 20,
                    width: 100,
                    height: 40,
                }),
                states: Vec::new(),
                actions: vec![AccessibilityAction {
                    index: 0,
                    name: "Click".to_string(),
                    description: "Clicks the button".to_string(),
                    keybinding: String::new(),
                }],
                value: None,
                text: None,
                supports_editable_text: false,
            },
        ];

        let compacted = compact_accessibility_tree(nodes);

        assert_eq!(compacted.len(), 3);
        assert_eq!(compacted[0].role, "application");
        assert_eq!(compacted[1].role, "frame");
        assert_eq!(compacted[2].role, "button");
        assert_eq!(compacted[2].parent_index, Some(1));
        assert_eq!(compacted[1].child_count, 1);
    }

    #[test]
    fn compact_accessibility_tree_drops_structural_noise() {
        let nodes = vec![
            AccessibilityNode {
                index: 0,
                parent_index: None,
                depth: 0,
                object_ref: ":1.0/root".to_string(),
                role: "application".to_string(),
                name: Some("demo-app".to_string()),
                description: None,
                child_count: 2,
                bounds: None,
                states: Vec::new(),
                actions: Vec::new(),
                value: None,
                text: None,
                supports_editable_text: false,
            },
            AccessibilityNode {
                index: 1,
                parent_index: Some(0),
                depth: 1,
                object_ref: ":1.1/frame".to_string(),
                role: "frame".to_string(),
                name: Some("Demo Frame".to_string()),
                description: None,
                child_count: 2,
                bounds: None,
                states: Vec::new(),
                actions: Vec::new(),
                value: None,
                text: None,
                supports_editable_text: false,
            },
            AccessibilityNode {
                index: 2,
                parent_index: Some(1),
                depth: 2,
                object_ref: ":1.2/tab".to_string(),
                role: "page tab".to_string(),
                name: Some("Hidden".to_string()),
                description: None,
                child_count: 0,
                bounds: None,
                states: Vec::new(),
                actions: Vec::new(),
                value: None,
                text: None,
                supports_editable_text: false,
            },
            AccessibilityNode {
                index: 3,
                parent_index: Some(1),
                depth: 2,
                object_ref: ":1.3/separator".to_string(),
                role: "separator".to_string(),
                name: None,
                description: None,
                child_count: 0,
                bounds: None,
                states: Vec::new(),
                actions: Vec::new(),
                value: None,
                text: None,
                supports_editable_text: false,
            },
        ];

        let compacted = compact_accessibility_tree(nodes);

        assert_eq!(compacted.len(), 3);
        assert_eq!(compacted[2].role, "page tab");
        assert_eq!(compacted[2].name.as_deref(), Some("Hidden"));
    }

    #[test]
    fn kde_clipboard_restore_delay_uses_minimum_for_short_text() {
        assert_eq!(
            kde_clipboard_restore_delay("short"),
            Duration::from_millis(KDE_CLIPBOARD_RESTORE_MIN_DELAY_MS)
        );
    }

    #[test]
    fn kde_clipboard_restore_delay_scales_and_caps_long_text() {
        let scaled_text = "x".repeat(1_000);
        assert_eq!(
            kde_clipboard_restore_delay(&scaled_text),
            Duration::from_millis(4_000)
        );

        let capped_text = "x".repeat(10_000);
        assert_eq!(
            kde_clipboard_restore_delay(&capped_text),
            Duration::from_millis(KDE_CLIPBOARD_RESTORE_MAX_DELAY_MS)
        );
    }

    #[tokio::test]
    async fn kde_clipboard_dbus_operation_times_out_when_pending() {
        let error = kde_clipboard_dbus_operation_with_timeout(
            "proxy creation",
            std::future::pending::<zbus::Result<()>>(),
            Duration::from_millis(1),
        )
        .await
        .unwrap_err();

        assert_eq!(error, "KDE clipboard proxy creation timed out");
    }

    #[test]
    fn absolute_mousemove_uses_coordinate_separator() {
        assert_eq!(
            absolute_mousemove_args(200, 300),
            vec![
                "mousemove".to_string(),
                "--absolute".to_string(),
                "--".to_string(),
                "200".to_string(),
                "300".to_string(),
            ]
        );
    }

    #[test]
    fn wheel_mousemove_uses_coordinate_separator_for_negative_values() {
        assert_eq!(
            wheel_mousemove_args(0, -3),
            vec![
                "mousemove".to_string(),
                "--wheel".to_string(),
                "--".to_string(),
                "0".to_string(),
                "-3".to_string(),
            ]
        );
    }

    #[test]
    fn pointer_actions_keep_pixel_coordinates_for_ydotool_absolute_moves() {
        assert_eq!(
            absolute_mousemove_args(1550, 930),
            vec![
                "mousemove".to_string(),
                "--absolute".to_string(),
                "--".to_string(),
                "1550".to_string(),
                "930".to_string(),
            ]
        );
    }

    #[test]
    fn coordinate_validation_accepts_capture_and_logical_monitor_spaces() {
        let capture_rect = (0, 0, 1920, 1080);
        let logical_rects = [(-1400, 0, 1280, 1024), (0, 0, 1920, 1080)];

        for point in [(0, 0), (1919, 1079), (-1400, 0), (-121, 1023)] {
            assert!(point_in_addressable_desktop(
                point,
                capture_rect,
                &logical_rects
            ));
        }
        for point in [(-1401, 0), (-120, 0), (0, -1), (1920, 0), (0, 1080)] {
            assert!(!point_in_addressable_desktop(
                point,
                capture_rect,
                &logical_rects
            ));
        }
    }

    #[tokio::test]
    async fn drag_rejects_an_invalid_endpoint_before_selecting_an_input_backend() {
        let backend = ComputerUseLinux::default();
        backend.cache_desktop_size(100, 80);

        let Json(output) = backend
            .drag(Parameters(DragParams {
                claim: ClaimContext::default(),
                start_x: 20,
                start_y: 20,
                end_x: 100,
                end_y: 40,
                ..Default::default()
            }))
            .await;

        assert!(!output.ok);
        assert!(output.message.contains("Invalid drag end point"));
        assert!(output.message.contains("no input was sent"));
    }

    #[test]
    fn key_sequence_presses_modifiers_around_key() {
        assert_eq!(
            key_sequence("Ctrl+Shift+P"),
            Some(vec![
                "29:1".to_string(),
                "42:1".to_string(),
                "25:1".to_string(),
                "25:0".to_string(),
                "42:0".to_string(),
                "29:0".to_string(),
            ])
        );
    }

    #[test]
    fn incompatible_ydotool_socket_does_not_suppress_portal_fallback() {
        assert!(should_prefer_portal_backend_by_default(
            true,
            ydotool_backend_available_from(true, false)
        ));
        assert!(!should_prefer_portal_backend_by_default(
            true,
            ydotool_backend_available_from(true, true)
        ));
        assert!(!should_prefer_portal_backend_by_default(
            false,
            ydotool_backend_available_from(true, false)
        ));
    }

    #[test]
    fn portal_delivery_failure_is_explicitly_not_replayed() {
        let received = Some(serde_json::json!({"x": 10, "y": 20}));
        assert_eq!(
            portal_action_delivery_failure(
                "click",
                &PortalActionError::MayHaveDelivered(anyhow::anyhow!("D-Bus reply was lost")),
                received.clone(),
            ),
            ActionOutput {
                ok: false,
                implemented: true,
                action: "click".to_string(),
                message: "Remote desktop portal click failed after the action attempt began. It may have been partially delivered, so it was not replayed through ydotool: D-Bus reply was lost".to_string(),
                received,
            }
        );
    }

    #[test]
    fn portal_action_phase_controls_ydotool_fallback() {
        let cases = [
            (
                PortalActionError::PreDispatch(anyhow::anyhow!("proxy setup failed")),
                true,
            ),
            (
                PortalActionError::MayHaveDelivered(anyhow::anyhow!("D-Bus reply was lost")),
                false,
            ),
        ];
        for (error, expected) in cases {
            assert_eq!(error.can_fallback_to_ydotool(), expected);
        }
    }

    #[tokio::test]
    async fn portal_action_task_finishes_release_after_waiter_cancellation() {
        let backend = ComputerUseLinux::default();
        let pressed = Arc::new(tokio::sync::Notify::new());
        let continue_action = Arc::new(tokio::sync::Notify::new());
        let released = Arc::new(std::sync::atomic::AtomicBool::new(false));
        let waiter_backend = backend.clone();
        let waiter_pressed = Arc::clone(&pressed);
        let waiter_continue = Arc::clone(&continue_action);
        let waiter_released = Arc::clone(&released);
        let waiter = tokio::spawn(async move {
            waiter_backend
                .run_portal_pointer_action(async move {
                    waiter_pressed.notify_one();
                    waiter_continue.notified().await;
                    waiter_released.store(true, std::sync::atomic::Ordering::SeqCst);
                    Ok(())
                })
                .await
        });

        pressed.notified().await;
        waiter.abort();
        let _ = waiter.await;
        continue_action.notify_one();
        let completion = tokio::time::timeout(Duration::from_secs(1), async {
            while !released.load(std::sync::atomic::Ordering::SeqCst) {
                tokio::task::yield_now().await;
            }
        })
        .await;
        assert!(completion.is_ok(), "detached portal action did not finish");
    }

    #[test]
    fn unavailable_portal_coordinate_transform_reports_that_no_input_was_sent() {
        let received = Some(serde_json::json!({"start_x": 10, "start_y": 20}));
        assert_eq!(
            portal_coordinate_input_unavailable("drag", received.clone()),
            ActionOutput {
                ok: false,
                implemented: true,
                action: "drag".to_string(),
                message: "Did not send drag input: the RemoteDesktop portal exposes no spec-defined transform from screenshot pixels to absolute pointer coordinates, and an allowed working ydotool backend is unavailable.".to_string(),
                received,
            }
        );
    }

    #[test]
    fn key_sequence_presses_bare_modifier() {
        assert_eq!(
            key_sequence("Super"),
            Some(vec!["125:1".to_string(), "125:0".to_string()])
        );
    }

    #[test]
    fn key_sequence_keeps_shortcuts_and_navigation_on_raw_events() {
        assert_eq!(
            key_sequence("Ctrl+L"),
            Some(vec![
                "29:1".to_string(),
                "38:1".to_string(),
                "38:0".to_string(),
                "29:0".to_string(),
            ])
        );
        assert_eq!(
            key_sequence("ArrowLeft"),
            Some(vec!["105:1".to_string(), "105:0".to_string()])
        );
        assert_eq!(
            key_sequence("Escape"),
            Some(vec!["1:1".to_string(), "1:0".to_string()])
        );
        assert_eq!(
            key_sequence("Enter"),
            Some(vec!["28:1".to_string(), "28:0".to_string()])
        );
    }

    #[test]
    fn ydotool_type_timeout_scales_with_text_length() {
        assert_eq!(ydotool_type_timeout("").as_secs(), 10);
        assert_eq!(ydotool_type_timeout("x").as_secs(), 11);
        assert_eq!(ydotool_type_timeout(&"x".repeat(200)).as_secs(), 20);
        assert_eq!(ydotool_type_timeout(&"x".repeat(500)).as_secs(), 35);
    }

    #[tokio::test]
    async fn ydotool_wait_drains_output_before_exit() {
        let mut command = tokio::process::Command::new("sh");
        command.args(["-c", "yes noisy | head -c 200000 >&2; exit 7"]);
        command.stdout(Stdio::piped());
        command.stderr(Stdio::piped());

        let output = wait_for_ydotool_output_with_timeout(
            command.spawn().expect("spawn noisy child"),
            Duration::from_secs(5),
        )
        .await
        .expect("child should exit before timeout");

        assert_eq!(output.status.code(), Some(7));
        assert!(output.stderr.len() >= 100_000);
    }

    #[test]
    fn ydotool_socket_selection_skips_unconnectable_candidates() {
        let dir =
            std::env::temp_dir().join(format!("computer-use-linux-server-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).expect("create temp server dir");
        let stale_socket = dir.join("stale.sock");
        std::fs::write(&stale_socket, b"not a socket").expect("write stale socket placeholder");
        let usable_socket = dir.join("usable.sock");
        let listener =
            std::os::unix::net::UnixListener::bind(&usable_socket).expect("bind usable socket");

        let selected = connectable_ydotool_socket_from(vec![stale_socket, usable_socket.clone()])
            .expect("usable socket should be selected");

        assert_eq!(selected, usable_socket);
        drop(listener);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn ydotool_socket_selection_accepts_datagram_socket() {
        let dir = std::env::temp_dir().join(format!(
            "computer-use-linux-server-dgram-{}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).expect("create temp server dir");
        let stale_socket = dir.join("stale.sock");
        std::fs::write(&stale_socket, b"not a socket").expect("write stale socket placeholder");
        let usable_socket = dir.join("usable.sock");
        let datagram =
            std::os::unix::net::UnixDatagram::bind(&usable_socket).expect("bind usable socket");

        let selected = connectable_ydotool_socket_from(vec![stale_socket, usable_socket.clone()])
            .expect("usable socket should be selected");

        assert_eq!(selected, usable_socket);
        drop(datagram);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn perform_action_defaults_to_first_snapshot_action() {
        let actions = [
            action(4, " open ", " Open item "),
            action(0, "delete", "Delete item"),
        ];
        let expected = ActionFingerprint::new("open", "Open item").unwrap();

        assert_eq!(
            snapshot_action_identity(&actions, None),
            Ok(expected.clone())
        );
        assert_eq!(
            snapshot_action_identity(&actions, Some("   ")),
            Ok(expected)
        );
    }

    #[test]
    fn text_action_precedes_snapshot_index_fallback() {
        let actions = [action(0, "delete", ""), action(7, "0", "literal")];
        let expected = ActionFingerprint::new("0", "literal").unwrap();

        assert_eq!(
            snapshot_action_identity(&actions, Some("0")),
            Ok(expected.clone())
        );
        assert_eq!(snapshot_action_identity(&actions, Some("7")), Ok(expected));
        assert!(snapshot_action_identity(&actions, Some("2")).is_err());
    }

    #[test]
    fn perform_action_rejects_ambiguous_or_unstable_snapshot_actions() {
        let duplicate = [
            action(0, "delete", "remove"),
            action(1, "archive", "remove"),
        ];
        assert!(snapshot_action_identity(&duplicate, Some("remove"))
            .unwrap_err()
            .contains("ambiguous"));

        let unnamed = [action(0, "", "")];
        assert!(snapshot_action_identity(&unnamed, None)
            .unwrap_err()
            .contains("no stable textual identity"));
        assert!(snapshot_action_identity(&unnamed, Some("missing"))
            .unwrap_err()
            .contains("not present"));
    }

    #[test]
    fn explicit_ydotool_socket_is_used_without_connectability_probe() {
        let key = "YDOTOOL_SOCKET";
        let original = std::env::var_os(key);
        std::env::set_var(key, " /does/not/exist.sock ");

        let selected = explicit_ydotool_socket();

        match original {
            Some(value) => std::env::set_var(key, value),
            None => std::env::remove_var(key),
        }

        assert_eq!(selected.as_deref(), Some("/does/not/exist.sock"));
    }

    #[test]
    fn element_identifier_must_belong_to_the_observation() {
        let backend = ComputerUseLinux::default();
        let observation_id = cache_observation(&backend, &[node(7, None)]);

        let error = backend
            .resolve_object_ref(
                Some(&observation_id),
                Some(7),
                Some(":1.99/org/a11y/atspi/accessible/3"),
                &ElementSelector::default(),
                ElementResolvePurpose::Action,
            )
            .unwrap_err();

        assert!(error.contains("does not belong to the supplied accessibility observation"));
    }

    #[test]
    fn element_index_resolves_to_cached_object_ref() {
        let backend = ComputerUseLinux::default();
        let observation_id = cache_observation(&backend, &[node(7, None)]);

        let object_ref = backend
            .resolve_object_ref(
                Some(&observation_id),
                Some(7),
                None,
                &ElementSelector::default(),
                ElementResolvePurpose::Action,
            )
            .unwrap();

        assert_eq!(object_ref, ":1.7/org/a11y/atspi/accessible/7");
        assert!(backend
            .resolve_object_ref(
                None,
                Some(7),
                None,
                &ElementSelector::default(),
                ElementResolvePurpose::Action,
            )
            .unwrap_err()
            .contains("observation_id is required"));
    }

    #[test]
    fn semantic_selector_resolves_unique_cached_node_by_role_and_name() {
        let backend = ComputerUseLinux::default();
        let mut search_entry = node(7, None);
        search_entry.role = "entry".to_string();
        search_entry.name = Some("Search files".to_string());
        search_entry.supports_editable_text = true;
        let observation_id = cache_observation(&backend, &[search_entry]);

        let object_ref = backend
            .resolve_object_ref(
                Some(&observation_id),
                None,
                None,
                &ElementSelector {
                    role: Some("entry"),
                    name: Some("search"),
                    ..Default::default()
                },
                ElementResolvePurpose::SetValue,
            )
            .unwrap();

        assert_eq!(object_ref, ":1.7/org/a11y/atspi/accessible/7");
    }

    #[test]
    fn semantic_selector_prefers_actionable_match() {
        let backend = ComputerUseLinux::default();
        let mut label = node(4, None);
        label.role = "label".to_string();
        label.name = Some("Close".to_string());
        let mut button = node_with_actions(7, None, vec![click_action()]);
        button.role = "push button".to_string();
        button.name = Some("Close".to_string());
        let observation_id = cache_observation(&backend, &[label, button]);

        let object_ref = backend
            .resolve_object_ref(
                Some(&observation_id),
                None,
                None,
                &ElementSelector {
                    name: Some("close"),
                    ..Default::default()
                },
                ElementResolvePurpose::Action,
            )
            .unwrap();

        assert_eq!(object_ref, ":1.7/org/a11y/atspi/accessible/7");
    }

    #[test]
    fn semantic_selector_prefers_editable_match() {
        let backend = ComputerUseLinux::default();
        let mut label = node(4, None);
        label.role = "label".to_string();
        label.name = Some("Search".to_string());
        let mut entry = node(7, None);
        entry.role = "entry".to_string();
        entry.name = Some("Search".to_string());
        entry.supports_editable_text = true;
        let observation_id = cache_observation(&backend, &[label, entry]);

        let object_ref = backend
            .resolve_object_ref(
                Some(&observation_id),
                None,
                None,
                &ElementSelector {
                    name: Some("search"),
                    ..Default::default()
                },
                ElementResolvePurpose::SetValue,
            )
            .unwrap();

        assert_eq!(object_ref, ":1.7/org/a11y/atspi/accessible/7");
    }

    #[test]
    fn semantic_selector_reports_ambiguous_matches() {
        let backend = ComputerUseLinux::default();
        let mut first = node_with_actions(7, None, vec![click_action()]);
        first.name = Some("Close".to_string());
        let mut second = node_with_actions(9, None, vec![click_action()]);
        second.name = Some("Close".to_string());
        let observation_id = cache_observation(&backend, &[first, second]);

        let error = backend
            .resolve_object_ref(
                Some(&observation_id),
                None,
                None,
                &ElementSelector {
                    name: Some("close"),
                    ..Default::default()
                },
                ElementResolvePurpose::Action,
            )
            .unwrap_err();

        assert!(error.contains("matched multiple cached nodes"));
        assert!(error.contains("element_index 7"));
        assert!(error.contains("element_index 9"));
    }

    #[test]
    fn semantic_click_selector_resolves_observation_bound_action() {
        let backend = ComputerUseLinux::default();
        let mut button = node_with_actions(
            7,
            Some(Bounds {
                x: 10,
                y: 20,
                width: 100,
                height: 40,
            }),
            vec![click_action()],
        );
        button.name = Some("Run".to_string());
        let observation_id = cache_window_observation(&backend, &[button]);

        let target = backend
            .resolve_observed_click_target(&ClickParams {
                observation_id: Some(observation_id),
                role: Some("button".to_string()),
                name: Some("run".to_string()),
                ..Default::default()
            })
            .unwrap();

        let ClickTarget::ObservedAction(target) = target else {
            panic!("expected an observation-bound AT-SPI action");
        };
        assert_eq!(target.object_ref, ":1.7/org/a11y/atspi/accessible/7");
        assert_eq!(
            target.action_identity,
            ActionFingerprint::new("Click", "Clicks the element").unwrap()
        );
    }

    #[test]
    fn semantic_click_selector_rejects_ambiguous_native_and_pointer_matches() {
        let backend = ComputerUseLinux::default();
        let mut action_only = node_with_actions(8, None, vec![click_action()]);
        action_only.name = Some("Run".to_string());
        let mut bounded = node_with_actions(
            7,
            Some(Bounds {
                x: 10,
                y: 20,
                width: 100,
                height: 40,
            }),
            vec![click_action()],
        );
        bounded.name = Some("Run".to_string());
        let observation_id = cache_window_observation(&backend, &[action_only, bounded]);

        let error = backend
            .resolve_observed_click_target(&ClickParams {
                observation_id: Some(observation_id),
                role: Some("button".to_string()),
                name: Some("run".to_string()),
                ..Default::default()
            })
            .unwrap_err();

        assert!(error.contains("matched multiple cached nodes"));
        assert!(error.contains("element_index 7"));
        assert!(error.contains("element_index 8"));
    }

    #[test]
    fn describe_focused_element_editable() {
        let element = FocusedElementSummary {
            role: "text".to_string(),
            name: Some("Message".to_string()),
            editable: true,
            states: vec!["focused".to_string()],
        };
        let described = describe_focused_element(&element, true);
        assert!(described.contains("editable"));
        assert!(!described.contains("WARNING"));
    }

    #[test]
    fn describe_focused_element_warns_on_non_editable_when_typing() {
        let element = FocusedElementSummary {
            role: "push button".to_string(),
            name: Some("OK".to_string()),
            editable: false,
            states: vec!["focused".to_string()],
        };
        let described = describe_focused_element(&element, true);
        assert!(described.contains("WARNING"));
        assert!(described.contains("not editable"));
    }

    #[test]
    fn describe_focused_element_no_warning_for_press_key() {
        let element = FocusedElementSummary {
            role: "push button".to_string(),
            name: None,
            editable: false,
            states: vec![],
        };
        let described = describe_focused_element(&element, false);
        assert!(!described.contains("WARNING"));
    }

    #[test]
    fn relative_scroll_translates_coordinates() {
        let mut params = ScrollParams {
            claim: ClaimContext::default(),
            observation_id: None,
            element_index: None,
            x: Some(10),
            y: Some(20),
            direction: "down".to_string(),
            pages: None,
            window_id: Some(1),
            pid: None,
            app_id: None,
            wm_class: None,
            window_title: None,
            relative: Some(true),
        };
        let focus = WindowFocusResult {
            requested_window: window_with_bounds(1, 100, 200, 800, 600),
            focused_window: None,
            app_focused: true,
            exact_window_focused: true,
            backend: "test".to_string(),
            note: String::new(),
        };
        apply_window_relative_scroll_coordinates(&mut params, &focus).unwrap();
        assert_eq!(params.x, Some(110));
        assert_eq!(params.y, Some(220));
    }

    #[test]
    fn window_targeted_scroll_defaults_to_window_center() {
        let mut params = ScrollParams {
            claim: ClaimContext::default(),
            observation_id: None,
            element_index: None,
            x: None,
            y: None,
            direction: "down".to_string(),
            pages: None,
            window_id: Some(1),
            pid: None,
            app_id: None,
            wm_class: None,
            window_title: None,
            relative: None,
        };
        let focus = WindowFocusResult {
            requested_window: window_with_bounds(1, 100, 200, 800, 600),
            focused_window: None,
            app_focused: true,
            exact_window_focused: true,
            backend: "test".to_string(),
            note: String::new(),
        };
        apply_window_center_scroll_point(&mut params, &focus).unwrap();
        assert_eq!(params.x, Some(500));
        assert_eq!(params.y, Some(500));
    }

    #[test]
    fn window_targeted_scroll_without_bounds_errors() {
        let mut params = ScrollParams {
            claim: ClaimContext::default(),
            observation_id: None,
            element_index: None,
            x: None,
            y: None,
            direction: "down".to_string(),
            pages: None,
            window_id: Some(1),
            pid: None,
            app_id: None,
            wm_class: None,
            window_title: None,
            relative: None,
        };
        let mut window = window_with_bounds(1, 0, 0, 1, 1);
        window.bounds = None;
        let focus = WindowFocusResult {
            requested_window: window,
            focused_window: None,
            app_focused: true,
            exact_window_focused: true,
            backend: "test".to_string(),
            note: String::new(),
        };
        let error = apply_window_center_scroll_point(&mut params, &focus).unwrap_err();
        assert!(error.contains("pass x/y explicitly"));
        assert_eq!(params.x, None);
        assert_eq!(params.y, None);
    }

    #[test]
    fn relative_scroll_rejects_out_of_bounds() {
        let mut params = ScrollParams {
            claim: ClaimContext::default(),
            observation_id: None,
            element_index: None,
            x: Some(801),
            y: Some(20),
            direction: "down".to_string(),
            pages: None,
            window_id: Some(1),
            pid: None,
            app_id: None,
            wm_class: None,
            window_title: None,
            relative: Some(true),
        };
        let focus = WindowFocusResult {
            requested_window: window_with_bounds(1, 100, 200, 800, 600),
            focused_window: None,
            app_focused: true,
            exact_window_focused: true,
            backend: "test".to_string(),
            note: String::new(),
        };
        assert!(apply_window_relative_scroll_coordinates(&mut params, &focus).is_err());
    }

    fn window_with_bounds(id: u64, x: i32, y: i32, width: u32, height: u32) -> WindowInfo {
        WindowInfo {
            window_id: id,
            title: None,
            app_id: None,
            wm_class: None,
            pid: None,
            bounds: Some(crate::windowing::WindowBounds {
                x: Some(x),
                y: Some(y),
                width,
                height,
            }),
            workspace: None,
            focused: true,
            hidden: false,
            client_type: None,
            backend: "test".to_string(),
            terminal: None,
        }
    }
}
