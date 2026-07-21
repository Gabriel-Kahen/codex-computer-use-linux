//! Persistent stdio bridge for the X11 companion plugin.
//!
//! Requests and response headers are one-line JSON. Successful capture headers
//! carry a byte count and are immediately followed by that many PNG bytes.

use crate::windowing::backends::x11_native;
use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::io::{self, BufRead, BufReader, BufWriter, Write};

const MAX_REQUEST_BYTES: usize = 4096;
const MAX_ERROR_CHARS: usize = 2048;
const MAX_COMPANION_PNG_BYTES: usize = 5 * 1024 * 1024;

#[derive(Deserialize)]
struct WorkerRequest {
    op: String,
    window_id: u64,
    expected_pid: Option<u32>,
    expected_width: Option<u32>,
    expected_height: Option<u32>,
}

#[derive(Serialize)]
struct WorkerResponse<'a> {
    protocol: u8,
    ok: bool,
    bytes: usize,
    width: Option<u32>,
    height: Option<u32>,
    authenticated_pid: Option<u32>,
    error: Option<&'a str>,
}

pub(crate) fn serve() -> Result<()> {
    let input = io::stdin();
    let output = io::stdout();
    serve_io(BufReader::new(input.lock()), BufWriter::new(output.lock()))
}

fn serve_io(mut input: impl BufRead, mut output: impl Write) -> Result<()> {
    let mut line = Vec::new();
    loop {
        let Some(oversized) = read_request_line(&mut input, &mut line)? else {
            return Ok(());
        };
        if oversized {
            write_error(&mut output, "native X11 worker request is too large")?;
            continue;
        }
        let result = serde_json::from_slice::<WorkerRequest>(&line)
            .context("invalid native X11 worker request")
            .and_then(handle_request);
        match result {
            Ok(WorkerResult::Pid(pid)) => write_header(
                &mut output,
                &WorkerResponse {
                    protocol: 1,
                    ok: true,
                    bytes: 0,
                    width: None,
                    height: None,
                    authenticated_pid: Some(pid),
                    error: None,
                },
            )?,
            Ok(WorkerResult::Capture(capture)) => {
                write_header(
                    &mut output,
                    &WorkerResponse {
                        protocol: 1,
                        ok: true,
                        bytes: capture.png.len(),
                        width: Some(capture.width),
                        height: Some(capture.height),
                        authenticated_pid: Some(capture.authenticated_pid),
                        error: None,
                    },
                )?;
                output
                    .write_all(&capture.png)
                    .context("failed to write native X11 capture bytes")?;
                output.flush()?;
            }
            Err(error) => write_error(&mut output, &format!("{error:#}"))?,
        }
    }
}

fn read_request_line(input: &mut impl BufRead, line: &mut Vec<u8>) -> Result<Option<bool>> {
    line.clear();
    let mut oversized = false;
    let mut read_any = false;
    loop {
        let available = input
            .fill_buf()
            .context("failed to read native X11 worker request")?;
        if available.is_empty() {
            return Ok(read_any.then_some(oversized));
        }
        read_any = true;
        let count = available
            .iter()
            .position(|byte| *byte == b'\n')
            .map_or(available.len(), |index| index + 1);
        if !oversized && line.len() + count <= MAX_REQUEST_BYTES {
            line.extend_from_slice(&available[..count]);
        } else {
            oversized = true;
        }
        let ended = available[count - 1] == b'\n';
        input.consume(count);
        if ended {
            return Ok(Some(oversized));
        }
    }
}

enum WorkerResult {
    Pid(u32),
    Capture(x11_native::NativeCapture),
}

fn handle_request(request: WorkerRequest) -> Result<WorkerResult> {
    match request.op.as_str() {
        "pid" => x11_native::authenticated_pid(request.window_id).map(WorkerResult::Pid),
        "capture" => {
            let expected_size = match (request.expected_width, request.expected_height) {
                (Some(width), Some(height)) if width > 0 && height > 0 => Some((width, height)),
                (None, None) => None,
                _ => anyhow::bail!("expected_width and expected_height must both be positive"),
            };
            let capture =
                x11_native::capture_window(request.window_id, request.expected_pid, expected_size)?
                    .context("native XComposite/XRes capture is unavailable in this X11 session")?;
            ensure_companion_payload_size(capture.png.len())?;
            Ok(WorkerResult::Capture(capture))
        }
        _ => anyhow::bail!("unknown native X11 worker operation"),
    }
}

fn ensure_companion_payload_size(size: usize) -> Result<()> {
    if size > MAX_COMPANION_PNG_BYTES {
        anyhow::bail!(
            "exact X11 capture is {size} bytes; maximum companion transport size is {MAX_COMPANION_PNG_BYTES} bytes"
        );
    }
    Ok(())
}

fn write_error(output: &mut impl Write, error: &str) -> Result<()> {
    let error = error.chars().take(MAX_ERROR_CHARS).collect::<String>();
    write_header(
        output,
        &WorkerResponse {
            protocol: 1,
            ok: false,
            bytes: 0,
            width: None,
            height: None,
            authenticated_pid: None,
            error: Some(&error),
        },
    )
}

fn write_header(output: &mut impl Write, response: &WorkerResponse<'_>) -> Result<()> {
    serde_json::to_writer(&mut *output, response)
        .context("failed to serialize native X11 worker response")?;
    output.write_all(b"\n")?;
    output.flush()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn worker_error_response_is_bounded_and_framed() {
        let mut output = Vec::new();
        write_error(&mut output, &"x".repeat(MAX_ERROR_CHARS + 100)).unwrap();
        assert!(output.ends_with(b"\n"));
        let response: serde_json::Value = serde_json::from_slice(&output).unwrap();
        assert_eq!(response["protocol"], 1);
        assert_eq!(response["ok"], false);
        assert_eq!(response["bytes"], 0);
        assert_eq!(
            response["error"].as_str().unwrap().chars().count(),
            MAX_ERROR_CHARS
        );
    }

    #[test]
    fn partial_capture_size_is_rejected_before_x11_access() {
        let request = WorkerRequest {
            op: "capture".to_string(),
            window_id: 1,
            expected_pid: None,
            expected_width: Some(10),
            expected_height: None,
        };
        assert!(handle_request(request)
            .err()
            .expect("partial dimensions must fail")
            .to_string()
            .contains("must both be positive"));
    }

    #[test]
    fn oversized_request_is_drained_without_desynchronizing_the_stream() {
        let mut input = vec![b'x'; MAX_REQUEST_BYTES + 1];
        input.extend_from_slice(b"\n{\"op\":\"bogus\",\"window_id\":1}\n");
        let mut output = Vec::new();

        serve_io(std::io::Cursor::new(input), &mut output).unwrap();

        let responses = output
            .split(|byte| *byte == b'\n')
            .filter(|line| !line.is_empty())
            .map(|line| serde_json::from_slice::<serde_json::Value>(line).unwrap())
            .collect::<Vec<_>>();
        assert_eq!(responses.len(), 2);
        assert!(responses[0]["error"]
            .as_str()
            .unwrap()
            .contains("too large"));
        assert!(responses[1]["error"]
            .as_str()
            .unwrap()
            .contains("unknown native X11 worker operation"));
    }

    #[test]
    fn companion_payload_limit_accepts_the_boundary_only() {
        assert!(ensure_companion_payload_size(MAX_COMPANION_PNG_BYTES).is_ok());
        assert!(ensure_companion_payload_size(MAX_COMPANION_PNG_BYTES + 1).is_err());
    }
}
