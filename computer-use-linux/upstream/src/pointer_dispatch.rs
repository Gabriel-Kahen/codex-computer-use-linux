use crate::accessibility_snapshot::{AccessibilitySnapshot, AccessibilitySnapshotStore};
use crate::atspi_tree::{live_bounds, Bounds};
use crate::windows::{focused_window, WindowFocusResult, WindowInfo, WindowTarget};
use anyhow::Result as AnyhowResult;
use std::future::Future;
use std::sync::Mutex;

#[derive(Clone, Copy, Debug)]
pub(crate) enum PointerDispatchBoundary {
    AbsolutePointer,
    CachedPortal,
    NewPortal,
    Ydotool,
}

pub(crate) async fn run_verified_pointer_dispatch<T>(
    _boundary: PointerDispatchBoundary,
    verification: impl Future<Output = Result<(), String>>,
    dispatch: impl Future<Output = T>,
) -> Result<T, String> {
    verification.await?;
    Ok(dispatch.await)
}

#[derive(Debug, Eq, PartialEq)]
pub(crate) struct ObservedElementPointer {
    pub(crate) observation_id: String,
    pub(crate) object_ref: String,
    pub(crate) point: (i32, i32),
}

#[derive(Debug, Eq, PartialEq)]
pub(crate) struct PointerDispatchVerification {
    pub(crate) exact_window_id: u64,
    pub(crate) expected_pid: Option<u32>,
    pub(crate) observed_element: Option<ObservedElementPointer>,
}

pub(crate) fn observed_element_pointer_target(
    observed_target: (u64, Option<u32>),
    observed_element: ObservedElementPointer,
    window: &WindowInfo,
) -> Result<(WindowTarget, PointerDispatchVerification), String> {
    if observed_target != (window.window_id, window.pid) {
        return Err(format!(
            "The accessibility observation does not match target window_id {}. Call get_app_state for that exact window and use its observation_id.",
            window.window_id
        ));
    }
    ensure_element_point_in_window(observed_element.point, window)?;
    let target = WindowTarget {
        window_id: Some(window.window_id),
        pid: window.pid,
        ..Default::default()
    };
    Ok((
        target,
        PointerDispatchVerification {
            exact_window_id: window.window_id,
            expected_pid: window.pid,
            observed_element: Some(observed_element),
        },
    ))
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
        observed_element: None,
    })
}

pub(crate) fn verify_pointer_dispatch_state<'a>(
    verification: &PointerDispatchVerification,
    focused: Option<&'a WindowInfo>,
) -> Result<&'a WindowInfo, String> {
    if let Some(window) = focused.filter(|window| {
        window.window_id == verification.exact_window_id && window.pid == verification.expected_pid
    }) {
        Ok(window)
    } else {
        Err(format!(
            "Did not send pointer input because exact window_id {} lost focus or changed process identity before injection.",
            verification.exact_window_id
        ))
    }
}

pub(crate) async fn verify_pointer_dispatch(
    verification: Option<&PointerDispatchVerification>,
    snapshots: &Mutex<AccessibilitySnapshotStore>,
) -> Result<(), String> {
    verify_pointer_dispatch_with(
        verification,
        |observation_id| resolve_snapshot(snapshots, observation_id),
        |object_ref| async move { live_bounds(&object_ref).await },
        focused_window,
    )
    .await
}

async fn verify_pointer_dispatch_with<Snapshot, LiveBounds, LiveBoundsFuture, Focus, FocusFuture>(
    verification: Option<&PointerDispatchVerification>,
    snapshot: Snapshot,
    live_bounds: LiveBounds,
    focus: Focus,
) -> Result<(), String>
where
    Snapshot: FnOnce(&str) -> Result<AccessibilitySnapshot, String>,
    LiveBounds: FnOnce(String) -> LiveBoundsFuture,
    LiveBoundsFuture: Future<Output = AnyhowResult<Bounds>>,
    Focus: FnOnce() -> FocusFuture,
    FocusFuture: Future<Output = AnyhowResult<Option<WindowInfo>>>,
{
    let Some(verification) = verification else {
        return Ok(());
    };
    let live_bounds = match &verification.observed_element {
        Some(observed) => Some(
            live_bounds(observed.object_ref.clone())
                .await
                .map_err(|error| format!("Could not verify live element bounds: {error:#}"))?,
        ),
        None => None,
    };
    let focused = focus()
        .await
        .map_err(|error| format!("Could not re-verify target-window focus: {error:#}"))?;
    verify_pointer_dispatch_snapshot_state(verification, focused.as_ref(), snapshot)?;
    if let (Some(observed), Some(bounds)) = (&verification.observed_element, live_bounds) {
        ensure_element_point_in_live_bounds(observed.point, &bounds)?;
    }
    Ok(())
}

fn verify_pointer_dispatch_snapshot_state(
    verification: &PointerDispatchVerification,
    focused: Option<&WindowInfo>,
    snapshot: impl FnOnce(&str) -> Result<AccessibilitySnapshot, String>,
) -> Result<(), String> {
    let focused = verify_pointer_dispatch_state(verification, focused)?;
    if let Some(observed) = &verification.observed_element {
        let snapshot = snapshot(&observed.observation_id)?;
        let snapshot_point = snapshot
            .nodes()
            .iter()
            .find(|node| node.object_ref == observed.object_ref)
            .and_then(|node| node.bounds.as_ref())
            .and_then(bounds_center)
            .ok_or_else(|| {
                "The observed element no longer belongs to the supplied accessibility observation. Call get_app_state again."
                    .to_string()
            })?;
        if snapshot_point != observed.point {
            return Err(
                "The pointer target does not match the element position in the supplied accessibility observation. Call get_app_state again."
                    .to_string(),
            );
        }
        ensure_observation_pointer_target(&snapshot, focused)?;
        ensure_element_point_in_window(observed.point, focused)?;
    }
    Ok(())
}

fn resolve_snapshot(
    snapshots: &Mutex<AccessibilitySnapshotStore>,
    observation_id: &str,
) -> Result<AccessibilitySnapshot, String> {
    snapshots
        .lock()
        .map_err(|_| {
            "Could not read accessibility observations. Call get_app_state and retry.".to_string()
        })?
        .resolve(observation_id)
}

fn ensure_observation_pointer_target(
    snapshot: &AccessibilitySnapshot,
    window: &WindowInfo,
) -> Result<(), String> {
    if snapshot.pointer_target()? == (window.window_id, window.pid) {
        Ok(())
    } else {
        Err(format!(
            "The accessibility observation does not match target window_id {}. Call get_app_state for that exact window and use its observation_id.",
            window.window_id
        ))
    }
}

fn ensure_element_point_in_window(point: (i32, i32), window: &WindowInfo) -> Result<(), String> {
    // AT-SPI bounds and compositor window bounds are both logical desktop pixels.
    let inside = window.bounds.as_ref().is_some_and(|bounds| {
        let (Some(x), Some(y)) = (bounds.x, bounds.y) else {
            return false;
        };
        point_in_rect(
            point,
            i64::from(x),
            i64::from(y),
            i64::from(bounds.width),
            i64::from(bounds.height),
        )
    });
    if inside {
        Ok(())
    } else {
        Err(format!(
            "The observed element center cannot be proven inside exact target window_id {}. Call get_app_state for that window again.",
            window.window_id
        ))
    }
}

fn ensure_element_point_in_live_bounds(point: (i32, i32), bounds: &Bounds) -> Result<(), String> {
    if point_in_rect(
        point,
        i64::from(bounds.x),
        i64::from(bounds.y),
        i64::from(bounds.width),
        i64::from(bounds.height),
    ) {
        Ok(())
    } else {
        Err(
            "The observed element moved before pointer injection. Call get_app_state again."
                .to_string(),
        )
    }
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

fn point_in_rect(point: (i32, i32), x: i64, y: i64, width: i64, height: i64) -> bool {
    let (point_x, point_y) = (i64::from(point.0), i64::from(point.1));
    width > 0
        && height > 0
        && point_x >= x
        && point_y >= y
        && point_x < x + width
        && point_y < y + height
}

#[cfg(test)]
#[path = "pointer_dispatch_tests.rs"]
mod tests;
