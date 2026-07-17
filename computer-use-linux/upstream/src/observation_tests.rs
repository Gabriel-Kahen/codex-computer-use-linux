use super::*;

fn screenshot(width: u32, height: u32, changes: &[(u32, u32)]) -> RawScreenshotCapture {
    let mut image = image::RgbaImage::from_pixel(width, height, image::Rgba([20, 30, 40, 255]));
    for &(x, y) in changes {
        image.put_pixel(x, y, image::Rgba([240, 220, 200, 255]));
    }
    let mut bytes = Vec::new();
    image::DynamicImage::ImageRgba8(image)
        .write_to(&mut Cursor::new(&mut bytes), image::ImageFormat::Png)
        .unwrap();
    RawScreenshotCapture {
        mime_type: "image/png".to_string(),
        bytes,
        source: "test".to_string(),
        width,
        height,
    }
}

fn observe(
    tracker: &mut ObservationTracker,
    key: &str,
    frame: &RawScreenshotCapture,
    base: Option<&AdaptiveObservationPlan>,
    interval: u32,
    force: bool,
) -> AdaptiveObservationPlan {
    tracker
        .observe(
            key.to_string(),
            Some(frame),
            base.map(|plan| plan.metadata.checkpoint_id.as_str()),
            interval,
            force,
        )
        .unwrap()
}

#[test]
fn checkpoint_ids_are_isolated_and_unchanged_frames_omit_pixels() {
    let mut tracker = ObservationTracker::default();
    let frame = screenshot(300, 200, &[]);
    let first = observe(&mut tracker, "demo", &frame, None, 8, false);
    let unchanged = observe(&mut tracker, "demo", &frame, Some(&first), 8, false);
    assert_eq!(
        unchanged.metadata.visual_kind,
        VisualObservationKind::Unchanged
    );
    assert_eq!(
        unchanged.metadata.checkpoint_id,
        first.metadata.checkpoint_id
    );

    let mismatch = observe(&mut tracker, "demo", &frame, None, 8, false);
    assert_ne!(
        mismatch.metadata.checkpoint_id,
        first.metadata.checkpoint_id
    );
    let stale = observe(&mut tracker, "demo", &frame, Some(&first), 8, false);
    assert_eq!(stale.metadata.visual_kind, VisualObservationKind::Full);
    let other = observe(&mut tracker, "other", &frame, None, 8, false);
    let wrong_target = observe(&mut tracker, "demo", &frame, Some(&other), 8, false);
    assert_eq!(
        wrong_target.metadata.visual_kind,
        VisualObservationKind::Full
    );
}

#[test]
fn changes_are_checkpoint_relative_and_cropped() {
    let mut tracker = ObservationTracker::default();
    let before = screenshot(400, 300, &[]);
    let checkpoint = observe(&mut tracker, "demo", &before, None, 8, false);
    let after = screenshot(400, 300, &[(150, 150)]);
    let plan = observe(&mut tracker, "demo", &after, Some(&checkpoint), 8, false);
    assert_eq!(
        plan.metadata.regions,
        vec![ObservationRegion {
            x: 112,
            y: 112,
            width: 160,
            height: 160,
        }]
    );
    let later = screenshot(400, 300, &[(150, 150), (300, 150)]);
    let later = observe(&mut tracker, "demo", &later, Some(&checkpoint), 8, false);
    assert_eq!(
        later.metadata.checkpoint_id,
        checkpoint.metadata.checkpoint_id
    );
    assert_eq!(later.metadata.regions[0].width, 288);
}

#[test]
fn fragmented_damage_forces_a_full_frame() {
    let previous = FrameDigest {
        width: 1280,
        height: 1280,
        columns: 10,
        rows: 10,
        tile_hashes: vec![0; 100],
    };
    let mut current = previous.clone();
    for index in [0, 2, 4, 20, 22] {
        current.tile_hashes[index] = 1;
    }
    assert!(matches!(
        changed_regions(&previous, &current),
        Some(ChangedFrame::Full)
    ));
}

#[test]
fn adaptive_payloads_are_bounded_without_capping_legacy_images() {
    let raw = screenshot(2048, 2048, &[]);
    let options = ScreenshotPayloadOptions {
        max_width: Some(4096),
        max_height: Some(4096),
        ..Default::default()
    };
    let adaptive = prepare_visual_captures(&raw, &VisualPlan::Full, options).unwrap();
    let legacy = prepare_screenshot_payload(raw, options).unwrap();
    let image = &adaptive[0].1;
    assert_eq!((legacy.width, legacy.height), (2048, 2048));
    assert!(image.width < legacy.width);
    assert!(
        image.width.div_ceil(VISION_PATCH_SIZE) as usize
            * image.height.div_ceil(VISION_PATCH_SIZE) as usize
            <= MAX_TOTAL_VISION_PATCHES
    );
    assert!(image.width as usize * image.height as usize <= MAX_TOTAL_IMAGE_PIXELS);
}
