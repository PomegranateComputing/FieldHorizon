use std::fs;
use std::path::Path;

use anyhow::{Context, Result};
use serde::Deserialize;

/// Only the `oppositions` list matters to the Rust core -- it doesn't do
/// retrieval routing or evaluator keyword scoring, so `domains:` and
/// `source_expansions:` are simply ignored by serde (no
/// `deny_unknown_fields`), the same way Python's ontology_spec.py is the
/// only reader that needs the full shape.
#[derive(Debug, Deserialize)]
struct OntologyFile {
    #[serde(default)]
    oppositions: Vec<(String, String, String)>,
}

#[derive(Debug, Clone)]
pub struct OppositionTable {
    pairs: Vec<(String, String, String)>,
}

impl OppositionTable {
    /// The declared (category_a, category_b, reason) triples, in
    /// ontology.yaml's file order. Used to group-and-cross only opposed
    /// categories instead of comparing every node pair (review §4).
    pub fn pairs(&self) -> &[(String, String, String)] {
        &self.pairs
    }
}

pub fn read_ontology(path: &Path) -> Result<OppositionTable> {
    let raw = fs::read_to_string(path)
        .with_context(|| format!("failed to read ontology file {}", path.display()))?;

    let parsed: OntologyFile = serde_yaml::from_str(&raw)
        .with_context(|| format!("failed to parse ontology YAML {}", path.display()))?;

    Ok(OppositionTable {
        pairs: parsed.oppositions,
    })
}
