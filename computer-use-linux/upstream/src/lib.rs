mod abs_pointer;
mod accessibility_snapshot;
mod action_batch;
#[path = "atspi_tree.rs"]
mod atspi_tree_impl;
mod cli;
mod cosmic_helper;
mod desktop_transaction;
#[path = "diagnostics.rs"]
mod diagnostics_impl;
mod gnome_extension;
mod identity;
mod observation;
mod pointer_dispatch;
mod remote_desktop;
#[path = "screenshot.rs"]
mod screenshot_impl;
mod server;
mod terminal;
mod windowing;
mod windows;
mod ydotool;

pub mod atspi_tree {
    pub(crate) use crate::atspi_tree_impl::{
        focused_element_summary, list_accessible_apps, perform_action, perform_action_by_identity,
        set_element_value, snapshot_compact_tree, AccessibleAppSummary, ActionFingerprint,
        FocusedElementSummary, ValueSetInvocation,
    };
    pub use crate::atspi_tree_impl::{
        snapshot_tree, AccessibilityAction, AccessibilityNode, AccessibilityText,
        AccessibilityTextSelection, AccessibilityValue, Bounds,
    };
}

pub mod diagnostics {
    pub use crate::diagnostics_impl::{
        doctor_report, hydrate_session_bus_env, AccessibilityReport, CapabilityMap, Check,
        DoctorReport, InputReport, PlatformReport, PortalReport, PreferredBackends,
        ReadinessReport, WindowingReport,
    };
    pub(crate) use crate::diagnostics_impl::{setup_accessibility_report, SetupReport};
}

pub mod screenshot {
    pub(crate) use crate::screenshot_impl::{
        capture_screenshot, capture_screenshot_raw_recent, prepare_screenshot_payload,
        ScreenshotCapture, ScreenshotOutputFormat, ScreenshotPayloadOptions,
    };
    pub use crate::screenshot_impl::{capture_screenshot_raw, RawScreenshotCapture};
}

#[doc(hidden)]
pub async fn run_cli_from_env() -> anyhow::Result<()> {
    cli::run_from_env().await
}
