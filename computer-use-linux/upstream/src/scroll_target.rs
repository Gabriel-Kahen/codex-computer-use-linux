use crate::accessibility_snapshot::AccessibilitySnapshotStore;
use crate::pointer_dispatch::{
    bounds_center, observed_element_pointer_target, ObservedElementPointer,
    PointerDispatchVerification,
};
use crate::windows::{list_windows, resolve_window_target, WindowInfo, WindowTarget};
use std::sync::Mutex;

pub(crate) struct ScrollTargetRequest<'a> {
    pub(crate) observation_id: Option<&'a str>,
    pub(crate) element_index: Option<u32>,
    pub(crate) x: Option<i32>,
    pub(crate) y: Option<i32>,
    pub(crate) relative: bool,
    pub(crate) window_target: Option<WindowTarget>,
}

#[derive(Clone, Debug)]
pub(crate) struct ObservedScrollTarget {
    point: (i32, i32),
    window_target: WindowTarget,
    expected_window_id: u64,
    expected_pid: Option<u32>,
    observed_element: ObservedElementPointer,
}

impl ObservedScrollTarget {
    pub(crate) fn point(&self) -> (i32, i32) {
        self.point
    }

    pub(crate) fn window_id(&self) -> u64 {
        self.expected_window_id
    }

    pub(crate) async fn prepare(
        &self,
    ) -> Result<(WindowTarget, PointerDispatchVerification), String> {
        let windows = list_windows()
            .await
            .map_err(|error| format!("Could not verify the element's target window: {error:#}"))?;
        self.prepare_from_windows(&windows)
    }

    fn prepare_from_windows(
        &self,
        windows: &[WindowInfo],
    ) -> Result<(WindowTarget, PointerDispatchVerification), String> {
        let window = resolve_window_target(windows, &self.window_target)
            .map_err(|error| format!("Could not verify the element's target window: {error:#}"))?;
        self.prepare_for_window(window)
    }

    fn prepare_for_window(
        &self,
        window: &WindowInfo,
    ) -> Result<(WindowTarget, PointerDispatchVerification), String> {
        observed_element_pointer_target(
            (self.expected_window_id, self.expected_pid),
            self.observed_element.clone(),
            window,
        )
    }
}

pub(crate) fn resolve_observed_scroll_target(
    snapshots: &Mutex<AccessibilitySnapshotStore>,
    request: ScrollTargetRequest<'_>,
) -> Result<Option<ObservedScrollTarget>, String> {
    match (request.x, request.y) {
        (Some(_), None) | (None, Some(_)) => {
            return Err("Coordinate scrolls require both x and y.".to_string());
        }
        (Some(_), Some(_)) if request.element_index.is_some() => {
            return Err("Do not combine scroll coordinates with element_index.".to_string());
        }
        _ => {}
    }
    let observation_id = request
        .observation_id
        .map(str::trim)
        .filter(|value| !value.is_empty());
    let Some(element_index) = request.element_index else {
        return if observation_id.is_some() {
            Err("observation_id is only valid with element_index for scroll.".to_string())
        } else {
            Ok(None)
        };
    };
    if request.relative {
        return Err(
            "relative=true is not supported for element-targeted scrolls because observed element bounds already use absolute desktop coordinates."
                .to_string(),
        );
    }
    let observation_id = observation_id.ok_or_else(|| {
        "observation_id is required for element-targeted scrolls. Call get_app_state and pass the returned observation_id."
            .to_string()
    })?;
    let snapshot = snapshots
        .lock()
        .map_err(|_| {
            "Could not read accessibility observations. Call get_app_state and retry.".to_string()
        })?
        .resolve(observation_id)?;
    let (expected_window_id, expected_pid) = snapshot.pointer_target()?;
    if request.window_target.as_ref().is_some_and(|target| {
        target
            .window_id
            .is_some_and(|window_id| window_id != expected_window_id)
            || target.pid.is_some_and(|pid| Some(pid) != expected_pid)
    }) {
        return Err(
            "The element observation does not match the requested target window.".to_string(),
        );
    }
    let node = snapshot
        .nodes()
        .iter()
        .find(|node| node.index == element_index)
        .ok_or_else(|| {
            format!(
                "No accessibility node for element_index {element_index} exists in the supplied observation. Call get_app_state again."
            )
        })?;
    let point = node.bounds.as_ref().and_then(bounds_center).ok_or_else(|| {
        format!(
            "No scrollable bounds in the accessibility observation for element_index {element_index}. Call get_app_state again and choose a node with positive bounds."
        )
    })?;
    let window_target = request.window_target.unwrap_or(WindowTarget {
        window_id: Some(expected_window_id),
        pid: expected_pid,
        ..Default::default()
    });
    Ok(Some(ObservedScrollTarget {
        point,
        window_target,
        expected_window_id,
        expected_pid,
        observed_element: ObservedElementPointer {
            observation_id: observation_id.to_string(),
            object_ref: node.object_ref.clone(),
            point,
        },
    }))
}

#[cfg(test)]
#[path = "scroll_target_tests.rs"]
mod tests;
