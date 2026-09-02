//! Managed-backend controls (FABLE Sec.8.4, Sec.14).
//!
//! The frontend can never pass a raw shell string here -- every field is a
//! discrete argument threaded straight into `std::process::Command`, which
//! never invokes a shell, so there is no command-injection surface. `launch`
//! validates `repo_root` looks like a real Field Horizon checkout before
//! spawning anything. `stop` only ever kills the child *this instance*
//! spawned (tracked by PID in `BackendState`) -- if nothing is tracked, it
//! errors instead of searching for and killing some unrelated process.

use std::path::Path;
use std::process::{Child, Command};
use std::sync::Mutex;

use tauri::State;

#[derive(Default)]
pub struct BackendState(Mutex<Option<Child>>);

fn validate_repo_root(repo_root: &str) -> Result<std::path::PathBuf, String> {
    let path = Path::new(repo_root)
        .canonicalize()
        .map_err(|e| format!("repo_root does not exist or is not readable: {e}"))?;
    if !path.join("config.yaml").is_file() {
        return Err(format!(
            "{} does not look like a Field Horizon checkout (no config.yaml)",
            path.display()
        ));
    }
    if !path.join("pyproject.toml").is_file() {
        return Err(format!(
            "{} does not look like a Field Horizon checkout (no pyproject.toml)",
            path.display()
        ));
    }
    Ok(path)
}

fn validate_python_bin(python_bin: &str) -> Result<std::path::PathBuf, String> {
    let path = Path::new(python_bin)
        .canonicalize()
        .map_err(|e| format!("python_bin does not exist or is not readable: {e}"))?;
    if !path.is_file() {
        return Err(format!("{} is not a file", path.display()));
    }
    Ok(path)
}

/// Builds the exact argv the managed backend is spawned with -- extracted
/// from `launch_backend` so a test can assert on it without actually
/// spawning a process. There is deliberately no `host`/`bind` parameter
/// anywhere in this list (or in `launch_backend`'s own signature): the
/// spawned `fieldhorizon.cli serve` always binds 127.0.0.1 via its own
/// hardcoded `HOST` constant (`fieldhorizon/server.py`), and nothing here
/// gives a caller -- malicious or not -- any argument position that could
/// override it. `port`/`token_file` each occupy exactly one fixed argv
/// slot (never shell-concatenated), so neither can smuggle in an extra flag.
fn backend_args(port: u16, token_file: &str) -> Vec<String> {
    vec![
        "-m".to_string(),
        "fieldhorizon.cli".to_string(),
        "serve".to_string(),
        "--port".to_string(),
        port.to_string(),
        "--token-file".to_string(),
        token_file.to_string(),
    ]
}

#[tauri::command]
pub fn launch_backend(
    state: State<BackendState>,
    python_bin: String,
    repo_root: String,
    port: u16,
    token_file: String,
) -> Result<(), String> {
    let mut guard = state.0.lock().map_err(|_| "backend state lock poisoned".to_string())?;
    if guard.is_some() {
        return Err("a managed backend is already running; stop it first".to_string());
    }

    let python = validate_python_bin(&python_bin)?;
    let root = validate_repo_root(&repo_root)?;

    let child = Command::new(python)
        .args(backend_args(port, &token_file))
        .current_dir(root)
        .spawn()
        .map_err(|e| format!("failed to spawn backend: {e}"))?;

    *guard = Some(child);
    Ok(())
}

#[tauri::command]
pub fn stop_backend(state: State<BackendState>) -> Result<(), String> {
    let mut guard = state.0.lock().map_err(|_| "backend state lock poisoned".to_string())?;
    match guard.take() {
        Some(mut child) => child.kill().map_err(|e| format!("failed to stop backend: {e}")),
        None => Err("no managed backend process to stop".to_string()),
    }
}

#[tauri::command]
pub fn backend_is_managed(state: State<BackendState>) -> Result<bool, String> {
    let mut guard = state.0.lock().map_err(|_| "backend state lock poisoned".to_string())?;
    if let Some(child) = guard.as_mut() {
        // try_wait reaps a process that already exited on its own (crash, Ctrl-C
        // outside our control) so a dead child doesn't report as still managed.
        match child.try_wait() {
            Ok(Some(_status)) => {
                *guard = None;
                Ok(false)
            }
            Ok(None) => Ok(true),
            Err(e) => Err(format!("failed to poll backend process: {e}")),
        }
    } else {
        Ok(false)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Implementation Brief IV, Phase UI-6 item 1: "loopback binding asserted
    /// by test for BOTH targets." The Python-side test suite already asserts
    /// `fieldhorizon.server.HOST == "127.0.0.1"`; this is the desktop-side
    /// half -- proving the managed-backend spawn path has no argument
    /// position, for any input, that could make the spawned server bind
    /// anywhere else.
    #[test]
    fn backend_args_never_contains_a_host_or_bind_override() {
        let forbidden = ["--host", "-h", "--bind", "0.0.0.0", "::"];
        for port in [1u16, 8777, 65535] {
            for token_file in ["/tmp/.fh_token", "weird --host 0.0.0.0 value", ""] {
                let args = backend_args(port, token_file);
                for needle in forbidden {
                    assert!(
                        !args.iter().any(|a| a == needle),
                        "backend_args({port}, {token_file:?}) produced a forbidden flag {needle:?}: {args:?}"
                    );
                }
            }
        }
    }

    #[test]
    fn backend_args_has_the_expected_fixed_shape() {
        let args = backend_args(8777, "/tmp/.fh_token");
        assert_eq!(
            args,
            vec![
                "-m", "fieldhorizon.cli", "serve", "--port", "8777", "--token-file", "/tmp/.fh_token",
            ]
        );
    }

    #[test]
    fn backend_args_treats_a_hostile_token_file_as_one_opaque_argument() {
        // Command::args never shell-parses -- a value containing spaces or
        // flag-like text still lands as exactly one argv element, never
        // split into additional arguments the spawned process could
        // reinterpret.
        let hostile = "x --host 0.0.0.0 --port 1";
        let args = backend_args(8777, hostile);
        assert_eq!(args.last().map(String::as_str), Some(hostile));
        assert_eq!(args.len(), 7);
    }

    #[test]
    fn validate_repo_root_rejects_a_directory_missing_the_marker_files() {
        let tmp = std::env::temp_dir().join(format!("fh-security-test-{}", std::process::id()));
        std::fs::create_dir_all(&tmp).unwrap();
        let result = validate_repo_root(tmp.to_str().unwrap());
        std::fs::remove_dir_all(&tmp).ok();
        assert!(result.is_err());
    }

    #[test]
    fn validate_repo_root_rejects_a_nonexistent_path() {
        assert!(validate_repo_root("/no/such/path/at/all/hopefully").is_err());
    }

    #[test]
    fn validate_python_bin_rejects_a_nonexistent_path() {
        assert!(validate_python_bin("/no/such/interpreter/at/all/hopefully").is_err());
    }
}
