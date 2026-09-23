use std::env;

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub(crate) struct PointerInputBackends {
    pub(crate) abs_pointer: bool,
    pub(crate) ydotool: bool,
}

impl PointerInputBackends {
    pub(crate) fn any(self) -> bool {
        self.abs_pointer || self.ydotool
    }
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub(crate) struct PointerInputOverrides {
    pub(crate) abs_pointer_disabled: bool,
    pub(crate) portal_pointer_forced: bool,
    pub(crate) ydotool_pointer_forced: bool,
}

impl PointerInputOverrides {
    pub(crate) fn from_env() -> Self {
        Self {
            abs_pointer_disabled: env_flag_enabled("CU_DISABLE_ABS_POINTER"),
            portal_pointer_forced: env_flag_enabled("COMPUTER_USE_LINUX_FORCE_PORTAL_POINTER"),
            ydotool_pointer_forced: env_flag_enabled("COMPUTER_USE_LINUX_FORCE_YDOTOOL_POINTER"),
        }
    }

    pub(crate) fn allows_ydotool(self) -> bool {
        self.ydotool_pointer_forced || !self.portal_pointer_forced
    }
}

pub(crate) fn effective_pointer_input_backends(
    available: PointerInputBackends,
    overrides: PointerInputOverrides,
) -> PointerInputBackends {
    PointerInputBackends {
        abs_pointer: available.abs_pointer && !overrides.abs_pointer_disabled,
        ydotool: available.ydotool && overrides.allows_ydotool(),
    }
}

fn env_flag_enabled(key: &str) -> bool {
    env::var(key).ok().as_deref() == Some("1")
}

#[cfg(test)]
#[path = "input_policy_tests.rs"]
mod tests;
