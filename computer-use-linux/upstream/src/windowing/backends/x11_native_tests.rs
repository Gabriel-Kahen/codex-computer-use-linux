use super::*;

#[test]
fn parses_icccm_wm_class_fields() {
    assert_eq!(
        parse_wm_class(b"Navigator\0firefox\0"),
        (Some("Navigator".to_string()), Some("firefox".to_string()))
    );
    assert_eq!(
        parse_wm_class(b"terminal\0"),
        (Some("terminal".to_string()), Some("terminal".to_string()))
    );
}

#[test]
fn cleans_bounded_x11_text_values() {
    assert_eq!(
        clean_bytes(b"  Firefox \0".to_vec()).as_deref(),
        Some("Firefox")
    );
    assert_eq!(clean_bytes(b"\0".to_vec()), None);
}
