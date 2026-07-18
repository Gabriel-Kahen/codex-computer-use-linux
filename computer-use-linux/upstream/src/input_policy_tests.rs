use super::*;

#[test]
fn effective_backends_follow_pointer_override_precedence() {
    let cases = [
        (
            PointerInputBackends {
                abs_pointer: true,
                ydotool: false,
            },
            PointerInputOverrides {
                abs_pointer_disabled: true,
                ..Default::default()
            },
            PointerInputBackends::default(),
        ),
        (
            PointerInputBackends {
                abs_pointer: false,
                ydotool: true,
            },
            PointerInputOverrides {
                portal_pointer_forced: true,
                ..Default::default()
            },
            PointerInputBackends::default(),
        ),
        (
            PointerInputBackends {
                abs_pointer: false,
                ydotool: true,
            },
            PointerInputOverrides {
                portal_pointer_forced: true,
                ydotool_pointer_forced: true,
                ..Default::default()
            },
            PointerInputBackends {
                abs_pointer: false,
                ydotool: true,
            },
        ),
    ];

    for (available, overrides, expected) in cases {
        assert_eq!(
            effective_pointer_input_backends(available, overrides),
            expected
        );
    }
}
