use crate::windows::{focused_window, WindowFocusResult, WindowInfo, WindowTarget};

#[derive(Debug, Eq, PartialEq)]
pub(crate) struct PointerDispatchVerification {
    pub(crate) exact_window_id: u64,
    pub(crate) expected_pid: Option<u32>,
}

pub(crate) fn pointer_dispatch_verification(
    target: &WindowTarget,
    relative: Option<bool>,
    focus: Option<&WindowFocusResult>,
) -> Option<PointerDispatchVerification> {
    if !target.requires_exact_focus() || relative == Some(true) {
        return None;
    }
    let requested = &focus?.requested_window;
    Some(PointerDispatchVerification {
        exact_window_id: requested.window_id,
        expected_pid: requested.pid,
    })
}

pub(crate) async fn verify_pointer_dispatch(
    verification: Option<&PointerDispatchVerification>,
) -> Result<(), String> {
    let Some(verification) = verification else {
        return Ok(());
    };
    let focused = focused_window()
        .await
        .map_err(|error| format!("Could not re-verify target-window focus: {error:#}"))?;
    verify_pointer_dispatch_state(verification, focused.as_ref())
}

fn verify_pointer_dispatch_state(
    verification: &PointerDispatchVerification,
    focused: Option<&WindowInfo>,
) -> Result<(), String> {
    if focused.is_some_and(|window| {
        window.window_id == verification.exact_window_id && window.pid == verification.expected_pid
    }) {
        Ok(())
    } else {
        Err(format!(
            "Did not send pointer input because exact window_id {} lost focus or changed process identity before injection.",
            verification.exact_window_id
        ))
    }
}

#[cfg(test)]
#[path = "pointer_dispatch_tests.rs"]
mod tests;
