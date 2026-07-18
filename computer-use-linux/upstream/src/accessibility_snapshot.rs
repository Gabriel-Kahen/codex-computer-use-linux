use crate::atspi_tree::AccessibilityNode;
use std::collections::VecDeque;
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const MAX_ACCESSIBILITY_TARGETS: usize = 16;
const ACCESSIBILITY_SNAPSHOT_TTL: Duration = Duration::from_secs(10 * 60);

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) enum AccessibilitySnapshotTarget {
    Window { window_id: u64, pid: Option<u32> },
    Process(u32),
    Application(String),
    Desktop,
}

impl AccessibilitySnapshotTarget {
    pub(crate) fn application(value: &str) -> Self {
        Self::Application(value.trim().to_ascii_lowercase())
    }

    fn same_scope(&self, other: &Self) -> bool {
        match (self, other) {
            (
                Self::Window { window_id, .. },
                Self::Window {
                    window_id: other_window_id,
                    ..
                },
            ) => window_id == other_window_id,
            (Self::Process(pid), Self::Process(other_pid)) => pid == other_pid,
            (Self::Application(name), Self::Application(other_name)) => name == other_name,
            (Self::Desktop, Self::Desktop) => true,
            _ => false,
        }
    }
}

struct StoredSnapshot {
    id: String,
    captured_at: Instant,
    target: AccessibilitySnapshotTarget,
    nodes: Arc<[AccessibilityNode]>,
}

#[derive(Clone, Debug)]
pub(crate) struct AccessibilitySnapshot {
    nodes: Arc<[AccessibilityNode]>,
}

impl AccessibilitySnapshot {
    pub(crate) fn nodes(&self) -> &[AccessibilityNode] {
        &self.nodes
    }
}

pub(crate) struct AccessibilitySnapshotStore {
    snapshots: VecDeque<StoredSnapshot>,
    max_targets: usize,
    ttl: Duration,
    nonce: u64,
    generation: u64,
}

impl Default for AccessibilitySnapshotStore {
    fn default() -> Self {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos() as u64;
        Self {
            snapshots: VecDeque::new(),
            max_targets: MAX_ACCESSIBILITY_TARGETS,
            ttl: ACCESSIBILITY_SNAPSHOT_TTL,
            nonce: nanos ^ u64::from(std::process::id()).rotate_left(17),
            generation: 0,
        }
    }
}

impl AccessibilitySnapshotStore {
    pub(crate) fn record(
        &mut self,
        target: AccessibilitySnapshotTarget,
        nodes: &[AccessibilityNode],
    ) -> String {
        self.record_at(target, nodes, Instant::now())
    }

    pub(crate) fn invalidate(&mut self, target: &AccessibilitySnapshotTarget) {
        self.snapshots
            .retain(|snapshot| !snapshot.target.same_scope(target));
    }

    pub(crate) fn resolve(
        &mut self,
        observation_id: &str,
    ) -> Result<AccessibilitySnapshot, String> {
        self.resolve_at(observation_id, Instant::now())
    }

    fn record_at(
        &mut self,
        target: AccessibilitySnapshotTarget,
        nodes: &[AccessibilityNode],
        now: Instant,
    ) -> String {
        self.purge_expired(now);
        self.invalidate(&target);
        while self.snapshots.len() >= self.max_targets {
            self.snapshots.pop_front();
        }
        self.generation = self.generation.wrapping_add(1);
        let id = format!("obs_{:016x}_{:016x}", self.nonce, self.generation);
        self.snapshots.push_back(StoredSnapshot {
            id: id.clone(),
            captured_at: now,
            target,
            nodes: Arc::from(nodes),
        });
        debug_assert!(self
            .snapshots
            .back()
            .is_some_and(|snapshot| { snapshot.id == id && snapshot.nodes.len() == nodes.len() }));
        id
    }

    fn purge_expired(&mut self, now: Instant) {
        self.snapshots.retain(|snapshot| {
            now.checked_duration_since(snapshot.captured_at)
                .is_none_or(|age| age <= self.ttl)
        });
    }

    fn resolve_at(
        &mut self,
        observation_id: &str,
        now: Instant,
    ) -> Result<AccessibilitySnapshot, String> {
        self.purge_expired(now);
        self.snapshots
            .iter()
            .find(|snapshot| snapshot.id == observation_id)
            .map(|snapshot| AccessibilitySnapshot {
                nodes: Arc::clone(&snapshot.nodes),
            })
            .ok_or_else(|| {
                "The accessibility observation_id is missing, stale, or expired. Call get_app_state again and use the new observation_id."
                    .to_string()
            })
    }
}

#[cfg(test)]
#[path = "accessibility_snapshot_tests.rs"]
mod tests;
