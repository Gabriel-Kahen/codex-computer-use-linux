use std::future::Future;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use tokio::sync::Mutex;

static NEXT_TRANSACTION_ID: AtomicU64 = AtomicU64::new(1);

tokio::task_local! {
    static ACTIVE_DESKTOP_TRANSACTIONS: Vec<u64>;
}

struct DesktopTransactionInner {
    id: u64,
    lock: Mutex<()>,
}

/// Serializes in-process desktop mutations while allowing nested operations in the same task.
///
/// Tool handlers enter a transaction before changing focus, input, or window state.
/// Batch handlers keep that scope active while they call the same guarded handlers,
/// so nested actions inherit the transaction instead of reacquiring its mutex.
#[derive(Clone)]
pub(crate) struct DesktopTransaction {
    inner: Arc<DesktopTransactionInner>,
}

impl Default for DesktopTransaction {
    fn default() -> Self {
        Self {
            inner: Arc::new(DesktopTransactionInner {
                id: NEXT_TRANSACTION_ID.fetch_add(1, Ordering::Relaxed),
                lock: Mutex::new(()),
            }),
        }
    }
}

impl DesktopTransaction {
    pub(crate) async fn run<T, F, Fut>(&self, operation: F) -> T
    where
        T: Send + 'static,
        F: FnOnce() -> Fut + Send + 'static,
        Fut: Future<Output = T> + Send + 'static,
    {
        let id = self.inner.id;
        if let Ok(mut active) = ACTIVE_DESKTOP_TRANSACTIONS.try_with(Clone::clone) {
            if active.contains(&id) {
                return operation().await;
            }

            let _guard = self.inner.lock.lock().await;
            active.push(id);
            return ACTIVE_DESKTOP_TRANSACTIONS
                .scope(active, async move { operation().await })
                .await;
        }

        let inner = Arc::clone(&self.inner);
        match tokio::spawn(async move {
            let _guard = inner.lock.lock().await;
            ACTIVE_DESKTOP_TRANSACTIONS
                .scope(vec![id], async move { operation().await })
                .await
        })
        .await
        {
            Ok(result) => result,
            Err(error) if error.is_panic() => std::panic::resume_unwind(error.into_panic()),
            Err(error) => panic!("desktop transaction worker was cancelled: {error}"),
        }
    }
}

#[cfg(test)]
#[path = "desktop_transaction_tests.rs"]
mod tests;
