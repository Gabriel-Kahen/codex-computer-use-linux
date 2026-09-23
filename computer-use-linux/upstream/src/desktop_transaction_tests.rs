use super::*;
use tokio::sync::oneshot;
use tokio::time::{timeout, Duration};

#[tokio::test]
async fn another_action_waits_for_the_active_transaction() {
    let transaction = DesktopTransaction::default();
    let first_transaction = transaction.clone();
    let (first_entered_tx, first_entered_rx) = oneshot::channel();
    let (release_first_tx, release_first_rx) = oneshot::channel();
    let first = tokio::spawn(async move {
        first_transaction
            .run(move || async move {
                first_entered_tx.send(()).unwrap();
                release_first_rx.await.unwrap();
            })
            .await;
    });
    first_entered_rx.await.unwrap();

    let second_transaction = transaction.clone();
    let (second_started_tx, second_started_rx) = oneshot::channel();
    let (second_entered_tx, mut second_entered_rx) = oneshot::channel();
    let second = tokio::spawn(async move {
        second_started_tx.send(()).unwrap();
        second_transaction
            .run(move || async move {
                second_entered_tx.send(()).unwrap();
            })
            .await;
    });
    second_started_rx.await.unwrap();
    tokio::task::yield_now().await;

    assert!(matches!(
        second_entered_rx.try_recv(),
        Err(oneshot::error::TryRecvError::Empty)
    ));
    release_first_tx.send(()).unwrap();
    second_entered_rx.await.unwrap();
    first.await.unwrap();
    second.await.unwrap();
}

#[tokio::test]
async fn nested_batch_action_inherits_the_transaction() {
    let transaction = DesktopTransaction::default();
    let nested_transaction = transaction.clone();

    let result = timeout(
        Duration::from_secs(1),
        transaction.run(move || async move { nested_transaction.run(|| async { 42 }).await }),
    )
    .await
    .unwrap();

    assert_eq!(result, 42);
}

#[tokio::test]
async fn transaction_releases_after_failure() {
    let transaction = DesktopTransaction::default();
    let failure = transaction
        .run(|| async { Err::<(), _>("operation failed") })
        .await;
    assert_eq!(failure, Err("operation failed"));

    let result = transaction.run(|| async { 7 }).await;
    assert_eq!(result, 7);
}

#[tokio::test]
async fn cancellation_keeps_transaction_locked_until_operation_finishes() {
    let transaction = DesktopTransaction::default();
    let cancelled_transaction = transaction.clone();
    let (entered_tx, entered_rx) = oneshot::channel();
    let (release_tx, release_rx) = oneshot::channel::<()>();
    let (finished_tx, finished_rx) = oneshot::channel();
    let cancelled = tokio::spawn(async move {
        cancelled_transaction
            .run(move || async move {
                entered_tx.send(()).unwrap();
                let _ = release_rx.await;
                finished_tx.send(()).unwrap();
            })
            .await;
    });
    entered_rx.await.unwrap();
    cancelled.abort();
    assert!(cancelled.await.unwrap_err().is_cancelled());

    let next_transaction = transaction.clone();
    let (next_entered_tx, mut next_entered_rx) = oneshot::channel();
    let next = tokio::spawn(async move {
        next_transaction
            .run(move || async move {
                next_entered_tx.send(()).unwrap();
                7
            })
            .await
    });
    assert!(timeout(Duration::from_millis(50), &mut next_entered_rx)
        .await
        .is_err());

    release_tx.send(()).unwrap();
    finished_rx.await.unwrap();
    next_entered_rx.await.unwrap();
    assert_eq!(next.await.unwrap(), 7);
}
