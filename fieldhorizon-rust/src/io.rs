use std::fs;
use std::path::Path;

use anyhow::{Context, Result};

use crate::types::JsonEntry;

pub fn read_entries(path: &Path) -> Result<Vec<JsonEntry>> {
    let raw = fs::read_to_string(path)
        .with_context(|| format!("failed to read {}", path.display()))?;

    let entries: Vec<JsonEntry> = serde_json::from_str(&raw)
        .with_context(|| format!("failed to parse JSON {}", path.display()))?;

    Ok(entries)
}
