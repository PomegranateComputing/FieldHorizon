use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Deserialize)]
pub struct JsonEntry {
    pub id: String,
    pub group_name: Option<String>,
    pub category: Option<String>,
    pub tradition: Option<String>,
    pub statement: Option<String>,
    pub gloss: Option<String>,
    pub severity: Option<f64>,
    pub mutation_potential: Option<f64>,
    pub tags: Option<Vec<String>>,
}

#[derive(Debug, Clone, Serialize)]
pub struct PressureEdge {
    pub source_id: String,
    pub target_id: String,
    pub source_category: String,
    pub target_category: String,
    pub pressure_type: String,
    pub score: f64,
    pub reason: String,
}

#[derive(Debug, Serialize)]
pub struct CoreReport {
    pub node_count: usize,
    pub edge_count: usize,
    pub edges: Vec<PressureEdge>,
}
