use crate::screenshot::{
    prepare_screenshot_payload, RawScreenshotCapture, ScreenshotCapture, ScreenshotPayloadOptions,
};
use anyhow::{Context, Result};
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use std::collections::{hash_map::RandomState, HashSet, VecDeque};
use std::hash::{BuildHasher, DefaultHasher, Hash, Hasher};
use std::io::Cursor;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::OnceLock;

const TILE_SIZE: u32 = 128;
const TILE_PADDING: u32 = 16;
const MAX_CHANGED_REGIONS: usize = 4;
const MAX_CHANGED_TILE_PERCENT: usize = 35;
const MAX_REGION_PIXEL_PERCENT: u64 = 50;
const MAX_TRACKED_TARGETS: usize = 8;
const DEFAULT_TOTAL_IMAGE_BYTES: usize = 2 * 1024 * 1024;
const MAX_TOTAL_IMAGE_BYTES: usize = 4 * 1024 * 1024;
const MAX_TOTAL_IMAGE_PIXELS: usize = 4 * 1024 * 1024;
const VISION_PATCH_SIZE: u32 = 32;
const MAX_TOTAL_VISION_PATCHES: usize = 3_072;
pub(crate) const DEFAULT_CHECKPOINT_INTERVAL: u32 = 8;
static NEXT_CHECKPOINT_ID: AtomicU64 = AtomicU64::new(1);
static PROCESS_CHECKPOINT_NONCE: OnceLock<u64> = OnceLock::new();

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize, Serialize, JsonSchema)]
#[serde(rename_all = "snake_case")]
pub(crate) enum ObservationMode {
    Adaptive,
    Full,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, JsonSchema)]
#[serde(rename_all = "snake_case")]
pub(crate) enum VisualObservationKind {
    Full,
    ChangedRegions,
    Unchanged,
    Unavailable,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, JsonSchema)]
pub(crate) struct ObservationRegion {
    pub x: u32,
    pub y: u32,
    pub width: u32,
    pub height: u32,
}

#[derive(Debug, Clone, Serialize, JsonSchema)]
pub(crate) struct AdaptiveObservationMetadata {
    pub sequence: u64,
    pub checkpoint_id: String,
    pub visual_kind: VisualObservationKind,
    pub frame_width: Option<u32>,
    pub frame_height: Option<u32>,
    pub regions: Vec<ObservationRegion>,
}

#[derive(Debug)]
pub(crate) struct AdaptiveObservationPlan {
    pub metadata: AdaptiveObservationMetadata,
    pub visual: VisualPlan,
}

#[derive(Debug)]
pub(crate) enum VisualPlan {
    Full,
    Regions(Vec<ObservationRegion>),
    None,
}

#[derive(Debug, Default)]
pub(crate) struct ObservationTracker {
    targets: VecDeque<TargetState>,
}

#[derive(Debug)]
struct TargetState {
    key: String,
    sequence: u64,
    checkpoint_sequence: u64,
    checkpoint_id: String,
    checkpoint_frame: Option<FrameDigest>,
}

#[derive(Debug, Clone)]
struct FrameDigest {
    width: u32,
    height: u32,
    columns: u32,
    rows: u32,
    tile_hashes: Vec<u64>,
}

impl ObservationTracker {
    pub(crate) fn observe(
        &mut self,
        key: String,
        raw: Option<&RawScreenshotCapture>,
        base_checkpoint_id: Option<&str>,
        checkpoint_interval: u32,
        force_checkpoint: bool,
    ) -> Result<AdaptiveObservationPlan> {
        let previous = self.take_target(&key);
        let sequence = previous.as_ref().map_or(1, |state| state.sequence + 1);
        let current_frame = raw.map(frame_digest).transpose()?;
        let periodic_checkpoint = previous.as_ref().is_some_and(|state| {
            sequence.saturating_sub(state.checkpoint_sequence) >= u64::from(checkpoint_interval)
        });
        let checkpoint_mismatch = previous
            .as_ref()
            .is_some_and(|state| base_checkpoint_id != Some(state.checkpoint_id.as_str()));
        let topology_changed = match (
            previous
                .as_ref()
                .and_then(|state| state.checkpoint_frame.as_ref()),
            current_frame.as_ref(),
        ) {
            (Some(previous), Some(current)) => {
                previous.width != current.width || previous.height != current.height
            }
            (None, Some(_)) => true,
            _ => false,
        };
        let mut checkpoint = previous.is_none()
            || checkpoint_mismatch
            || force_checkpoint
            || periodic_checkpoint
            || topology_changed;

        let visual = match (raw, current_frame.as_ref()) {
            (Some(_), Some(_)) if checkpoint => VisualPlan::Full,
            (Some(_), Some(current)) => {
                let checkpoint_frame = previous
                    .as_ref()
                    .and_then(|state| state.checkpoint_frame.as_ref());
                match checkpoint_frame.and_then(|checkpoint| changed_regions(checkpoint, current)) {
                    Some(ChangedFrame::Unchanged) => VisualPlan::None,
                    Some(ChangedFrame::Regions(regions)) => VisualPlan::Regions(regions),
                    Some(ChangedFrame::Full) | None => {
                        checkpoint = true;
                        VisualPlan::Full
                    }
                }
            }
            _ => VisualPlan::None,
        };

        let checkpoint_sequence = if checkpoint {
            sequence
        } else {
            previous
                .as_ref()
                .map_or(sequence, |state| state.checkpoint_sequence)
        };
        let checkpoint_id = if checkpoint {
            let mut hasher = DefaultHasher::new();
            key.hash(&mut hasher);
            let key_hash = hasher.finish();
            let process_nonce = PROCESS_CHECKPOINT_NONCE
                .get_or_init(|| RandomState::new().hash_one(std::process::id()));
            let nonce = NEXT_CHECKPOINT_ID.fetch_add(1, Ordering::Relaxed);
            format!("cp-{process_nonce:016x}-{key_hash:016x}-{nonce:016x}")
        } else {
            previous
                .as_ref()
                .map_or_else(String::new, |state| state.checkpoint_id.clone())
        };
        let visual_kind = match &visual {
            VisualPlan::Full => VisualObservationKind::Full,
            VisualPlan::Regions(_) => VisualObservationKind::ChangedRegions,
            VisualPlan::None if raw.is_some() => VisualObservationKind::Unchanged,
            VisualPlan::None => VisualObservationKind::Unavailable,
        };
        let regions = match &visual {
            VisualPlan::Full => current_frame
                .as_ref()
                .map(|frame| full_region(frame.width, frame.height))
                .into_iter()
                .collect(),
            VisualPlan::Regions(regions) => regions.clone(),
            VisualPlan::None => Vec::new(),
        };
        let metadata = AdaptiveObservationMetadata {
            sequence,
            checkpoint_id: checkpoint_id.clone(),
            visual_kind,
            frame_width: current_frame.as_ref().map(|frame| frame.width),
            frame_height: current_frame.as_ref().map(|frame| frame.height),
            regions,
        };
        let checkpoint_frame = if checkpoint {
            current_frame
        } else {
            previous.and_then(|state| state.checkpoint_frame)
        };
        self.targets.push_back(TargetState {
            key,
            sequence,
            checkpoint_sequence,
            checkpoint_id,
            checkpoint_frame,
        });

        Ok(AdaptiveObservationPlan { metadata, visual })
    }

    fn take_target(&mut self, key: &str) -> Option<TargetState> {
        let target = self
            .targets
            .iter()
            .position(|state| state.key == key)
            .and_then(|index| self.targets.remove(index));
        if target.is_none() && self.targets.len() == MAX_TRACKED_TARGETS {
            self.targets.pop_front();
        }
        target
    }
}

pub(crate) fn prepare_visual_captures(
    raw: &RawScreenshotCapture,
    plan: &VisualPlan,
    options: ScreenshotPayloadOptions,
) -> Result<Vec<(ObservationRegion, ScreenshotCapture)>> {
    let regions = match plan {
        VisualPlan::Full => vec![full_region(raw.width, raw.height)],
        VisualPlan::Regions(regions) => regions.clone(),
        VisualPlan::None => return Ok(Vec::new()),
    };
    let mut options = options;
    let pixel_side = ((MAX_TOTAL_IMAGE_PIXELS / regions.len()) as f64).sqrt() as u32;
    let patch_side =
        ((MAX_TOTAL_VISION_PATCHES / regions.len()) as f64).sqrt() as u32 * VISION_PATCH_SIZE;
    let max_side = pixel_side.min(patch_side);
    options.max_width = Some(options.max_width.unwrap_or(max_side).min(max_side));
    options.max_height = Some(options.max_height.unwrap_or(max_side).min(max_side));
    if regions.len() > 1 {
        let aggregate_bytes = if options.max_bytes.is_some() {
            MAX_TOTAL_IMAGE_BYTES
        } else {
            DEFAULT_TOTAL_IMAGE_BYTES
        };
        let per_region_bytes = aggregate_bytes / regions.len();
        options.max_bytes = Some(
            options
                .max_bytes
                .unwrap_or(per_region_bytes)
                .clamp(1024, MAX_TOTAL_IMAGE_BYTES)
                .min(per_region_bytes),
        );
    }
    regions
        .into_iter()
        .map(|region| {
            let cropped = crop_raw(raw, region)?;
            let capture = prepare_screenshot_payload(cropped, options)?;
            Ok((region, capture))
        })
        .collect()
}

fn frame_digest(raw: &RawScreenshotCapture) -> Result<FrameDigest> {
    let image = image::load_from_memory(&raw.bytes)
        .context("failed to decode adaptive observation screenshot")?
        .to_rgba8();
    let (width, height) = image.dimensions();
    let columns = width.div_ceil(TILE_SIZE);
    let rows = height.div_ceil(TILE_SIZE);
    let mut tile_hashes = Vec::with_capacity((columns * rows) as usize);
    for tile_y in 0..rows {
        for tile_x in 0..columns {
            let x = tile_x * TILE_SIZE;
            let y = tile_y * TILE_SIZE;
            let width = TILE_SIZE.min(width - x);
            let height = TILE_SIZE.min(height - y);
            let mut hasher = DefaultHasher::new();
            for row in y..y + height {
                image.as_raw()[(row * image.width() * 4 + x * 4) as usize
                    ..(row * image.width() * 4 + (x + width) * 4) as usize]
                    .hash(&mut hasher);
            }
            tile_hashes.push(hasher.finish());
        }
    }
    Ok(FrameDigest {
        width,
        height,
        columns,
        rows,
        tile_hashes,
    })
}

enum ChangedFrame {
    Unchanged,
    Regions(Vec<ObservationRegion>),
    Full,
}

fn changed_regions(previous: &FrameDigest, current: &FrameDigest) -> Option<ChangedFrame> {
    if previous.columns != current.columns || previous.rows != current.rows {
        return None;
    }
    let changed = previous
        .tile_hashes
        .iter()
        .zip(&current.tile_hashes)
        .enumerate()
        .filter_map(|(index, (previous, current))| (previous != current).then_some(index))
        .collect::<HashSet<_>>();
    if changed.is_empty() {
        return Some(ChangedFrame::Unchanged);
    }
    if changed.len() * 100 > current.tile_hashes.len() * MAX_CHANGED_TILE_PERCENT {
        return Some(ChangedFrame::Full);
    }

    let mut remaining = changed;
    let mut regions = Vec::new();
    while let Some(&start) = remaining.iter().next() {
        let mut stack = vec![start];
        remaining.remove(&start);
        let (start_x, start_y) = tile_coordinates(start, current.columns);
        let (mut min_x, mut max_x, mut min_y, mut max_y) = (start_x, start_x, start_y, start_y);
        while let Some(index) = stack.pop() {
            let (x, y) = tile_coordinates(index, current.columns);
            min_x = min_x.min(x);
            max_x = max_x.max(x);
            min_y = min_y.min(y);
            max_y = max_y.max(y);
            for (neighbor_x, neighbor_y) in [
                (x.wrapping_sub(1), y),
                (x + 1, y),
                (x, y.wrapping_sub(1)),
                (x, y + 1),
            ] {
                if neighbor_x < current.columns && neighbor_y < current.rows {
                    let neighbor = (neighbor_y * current.columns + neighbor_x) as usize;
                    if remaining.remove(&neighbor) {
                        stack.push(neighbor);
                    }
                }
            }
        }
        regions.push(padded_region(
            min_x,
            max_x,
            min_y,
            max_y,
            current.width,
            current.height,
        ));
        if regions.len() > MAX_CHANGED_REGIONS {
            return Some(ChangedFrame::Full);
        }
    }
    regions.sort_by_key(|region| (region.y, region.x));
    let region_pixels = regions
        .iter()
        .map(|region| u64::from(region.width) * u64::from(region.height))
        .sum::<u64>();
    if region_pixels * 100
        > u64::from(current.width) * u64::from(current.height) * MAX_REGION_PIXEL_PERCENT
    {
        return Some(ChangedFrame::Full);
    }
    Some(ChangedFrame::Regions(regions))
}

fn tile_coordinates(index: usize, columns: u32) -> (u32, u32) {
    (index as u32 % columns, index as u32 / columns)
}

fn padded_region(
    min_tile_x: u32,
    max_tile_x: u32,
    min_tile_y: u32,
    max_tile_y: u32,
    frame_width: u32,
    frame_height: u32,
) -> ObservationRegion {
    let x = (min_tile_x * TILE_SIZE).saturating_sub(TILE_PADDING);
    let y = (min_tile_y * TILE_SIZE).saturating_sub(TILE_PADDING);
    let right = ((max_tile_x + 1) * TILE_SIZE)
        .min(frame_width)
        .saturating_add(TILE_PADDING)
        .min(frame_width);
    let bottom = ((max_tile_y + 1) * TILE_SIZE)
        .min(frame_height)
        .saturating_add(TILE_PADDING)
        .min(frame_height);
    ObservationRegion {
        x,
        y,
        width: right - x,
        height: bottom - y,
    }
}

fn full_region(width: u32, height: u32) -> ObservationRegion {
    ObservationRegion {
        x: 0,
        y: 0,
        width,
        height,
    }
}

fn crop_raw(raw: &RawScreenshotCapture, region: ObservationRegion) -> Result<RawScreenshotCapture> {
    let image = image::load_from_memory(&raw.bytes)
        .context("failed to decode adaptive observation region")?;
    let cropped = image.crop_imm(region.x, region.y, region.width, region.height);
    let mut bytes = Vec::new();
    cropped
        .write_to(&mut Cursor::new(&mut bytes), image::ImageFormat::Png)
        .context("failed to encode adaptive observation region")?;
    Ok(RawScreenshotCapture {
        mime_type: "image/png".to_string(),
        bytes,
        source: raw.source.clone(),
        width: region.width,
        height: region.height,
    })
}

#[cfg(test)]
#[path = "observation_tests.rs"]
mod tests;
