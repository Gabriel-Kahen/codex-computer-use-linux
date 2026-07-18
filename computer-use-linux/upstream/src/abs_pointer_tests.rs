use super::validate_point;

#[test]
fn accepts_only_pixels_inside_the_absolute_axis_range() {
    assert!(validate_point(1920, 1080, 0, 0).is_ok());
    assert!(validate_point(1920, 1080, 1919, 1079).is_ok());

    for point in [(-1, 0), (0, -1), (1920, 0), (0, 1080)] {
        let error = validate_point(1920, 1080, point.0, point.1).unwrap_err();
        assert!(error
            .to_string()
            .contains("outside the addressable desktop"));
    }
}
