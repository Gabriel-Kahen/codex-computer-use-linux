use super::*;

#[derive(Debug)]
pub(super) struct ObservedClickTarget {
    pub(super) observation_id: String,
    pub(super) object_ref: String,
    pub(super) point: (i32, i32),
    pub(super) window_id: u64,
    pub(super) pid: Option<u32>,
}

#[derive(Debug)]
pub(super) struct ObservedClickAction {
    pub(super) observation_id: String,
    pub(super) object_ref: String,
    pub(super) action_identity: ActionFingerprint,
    pub(super) window_id: u64,
}

impl ComputerUseLinux {
    pub(super) fn resolve_observed_click_target(
        &self,
        params: &ClickParams,
    ) -> std::result::Result<ClickTarget, String> {
        let selector = params.selector();
        let has_element_target = params.element_index.is_some() || !selector.is_empty();
        match (params.x, params.y) {
            (Some(x), Some(y)) => return Ok(ClickTarget::Coordinates(x, y)),
            (Some(_), None) | (None, Some(_)) => {
                return Err("Coordinate clicks require both x and y.".to_string());
            }
            (None, None) => {}
        }
        if !has_element_target {
            return Err("Pass x and y, element_index, or a semantic selector.".to_string());
        }
        if params.relative == Some(true) {
            return Err(
                "relative=true is not supported for element-targeted clicks because observed element bounds already use absolute desktop coordinates."
                    .to_string(),
            );
        }

        let observation_id = params
            .observation_id
            .as_deref()
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .ok_or_else(|| {
                "observation_id is required for element-based actions. Call get_app_state and pass the returned observation_id."
                    .to_string()
            })?;
        let snapshot = self.accessibility_snapshot(Some(observation_id))?;
        let (window_id, pid) = snapshot.pointer_target()?;
        if params.window_id.is_some_and(|id| id != window_id)
            || params.pid.is_some_and(|requested| Some(requested) != pid)
        {
            return Err(
                "The element observation does not match the requested target window.".to_string(),
            );
        }
        let bounds_error = |index| {
            format!(
                "No clickable bounds in the accessibility observation for element_index {index}. Call get_app_state again, or use perform_action with the same observation_id for a stable AT-SPI action."
            )
        };
        let semantic_fast_path = is_plain_left_click(params.button.as_deref(), params.click_count)
            && params.window_target().is_none();
        let purpose = if semantic_fast_path {
            ElementResolvePurpose::ObservedSemanticClick
        } else {
            ElementResolvePurpose::ObservedClick
        };
        let node = self
            .resolve_observed_node(
                Some(observation_id),
                params.element_index,
                None,
                &selector,
                purpose,
            )
            .map_err(|error| {
                match self.resolve_observed_node(
                    Some(observation_id),
                    params.element_index,
                    None,
                    &selector,
                    ElementResolvePurpose::Action,
                ) {
                    Ok(node) if node.bounds.as_ref().and_then(bounds_center).is_none() => {
                        bounds_error(node.index)
                    }
                    _ => error,
                }
            })?;
        let action_identity = node
            .actions
            .first()
            .filter(|action| semantic_fast_path && action.name.trim().eq_ignore_ascii_case("click"))
            .and_then(|action| ActionFingerprint::new(&action.name, &action.description));
        if let Some(action_identity) = action_identity {
            return Ok(ClickTarget::ObservedAction(ObservedClickAction {
                observation_id: observation_id.to_string(),
                object_ref: node.object_ref,
                action_identity,
                window_id,
            }));
        }
        let Some(point) = node.bounds.as_ref().and_then(bounds_center) else {
            return Err(bounds_error(node.index));
        };
        Ok(ClickTarget::ObservedCoordinates(ObservedClickTarget {
            observation_id: observation_id.to_string(),
            object_ref: node.object_ref,
            point,
            window_id,
            pid,
        }))
    }

    pub(super) fn verify_observed_click_action_freshness(
        &self,
        observed: &ObservedClickAction,
    ) -> std::result::Result<(), String> {
        self.accessibility_snapshot(Some(&observed.observation_id))
            .map(drop)
    }

    pub(super) async fn prepare_observed_click_target(
        &self,
        observed: &ObservedClickTarget,
        explicit_target: Option<WindowTarget>,
    ) -> std::result::Result<(WindowTarget, PointerDispatchVerification), String> {
        let target = explicit_target.unwrap_or(WindowTarget {
            window_id: Some(observed.window_id),
            pid: observed.pid,
            ..Default::default()
        });
        let windows = list_windows()
            .await
            .map_err(|error| format!("Could not verify the element's target window: {error:#}"))?;
        let window = resolve_window_target(&windows, &target)
            .map_err(|error| format!("Could not verify the element's target window: {error:#}"))?;
        observed_element_pointer_target(
            (observed.window_id, observed.pid),
            ObservedElementPointer {
                observation_id: observed.observation_id.clone(),
                object_ref: observed.object_ref.clone(),
                point: observed.point,
            },
            window,
        )
    }
}

#[cfg(test)]
#[path = "click_target_tests.rs"]
mod tests;
