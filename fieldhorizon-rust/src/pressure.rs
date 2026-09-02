use std::collections::{HashMap, HashSet};

use crate::ontology::OppositionTable;
use crate::types::{CoreReport, JsonEntry, PressureEdge};

fn cat(entry: &JsonEntry) -> String {
    entry.category.clone().unwrap_or_default()
}

fn score(a: &JsonEntry, b: &JsonEntry) -> f64 {
    let a_sev = a.severity.unwrap_or(0.0);
    let b_sev = b.severity.unwrap_or(0.0);
    let a_mut = a.mutation_potential.unwrap_or(0.0);
    let b_mut = b.mutation_potential.unwrap_or(0.0);

    let raw = ((a_sev + b_sev) / 2.0) + ((a_mut + b_mut) / 4.0);
    (raw * 1000.0).round() / 1000.0
}

fn push_edge(
    edges: &mut Vec<PressureEdge>,
    a: &JsonEntry,
    b: &JsonEntry,
    cat_a: &str,
    cat_b: &str,
    reason: &str,
) {
    edges.push(PressureEdge {
        source_id: a.id.clone(),
        target_id: b.id.clone(),
        source_category: cat_a.to_string(),
        target_category: cat_b.to_string(),
        pressure_type: "ontological_pressure".to_string(),
        score: score(a, b),
        reason: reason.to_string(),
    });
}

/// Edges only ever exist between the (~10) category pairs ontology.yaml
/// declares as opposed, so there is no reason to examine all N*(N-1)/2
/// entry pairs: group entries by category first, then cross only the
/// groups an opposition pair actually names (review §4).
pub fn build_pressure_report(
    entries: &[JsonEntry],
    limit: usize,
    oppositions: &OppositionTable,
) -> CoreReport {
    let mut groups: HashMap<String, Vec<&JsonEntry>> = HashMap::new();
    for entry in entries {
        let category = cat(entry);
        if !category.is_empty() {
            groups.entry(category).or_default().push(entry);
        }
    }

    let mut edges: Vec<PressureEdge> = Vec::new();
    let mut seen_pairs: HashSet<(String, String)> = HashSet::new();
    let empty: Vec<&JsonEntry> = Vec::new();

    for (cat_a, cat_b, reason) in oppositions.pairs() {
        let pair_key = if cat_a <= cat_b {
            (cat_a.clone(), cat_b.clone())
        } else {
            (cat_b.clone(), cat_a.clone())
        };
        if !seen_pairs.insert(pair_key) {
            continue;
        }

        let group_a = groups.get(cat_a).unwrap_or(&empty);
        let group_b = groups.get(cat_b).unwrap_or(&empty);

        if cat_a == cat_b {
            for i in 0..group_a.len() {
                for j in (i + 1)..group_a.len() {
                    push_edge(&mut edges, group_a[i], group_a[j], cat_a, cat_b, reason);
                }
            }
        } else {
            for a in group_a {
                for b in group_b {
                    push_edge(&mut edges, a, b, cat_a, cat_b, reason);
                }
            }
        }
    }

    edges.sort_by(|a, b| {
        b.score
            .partial_cmp(&a.score)
            .unwrap_or(std::cmp::Ordering::Equal)
    });

    let edge_count = edges.len();
    edges.truncate(limit);

    CoreReport {
        node_count: entries.len(),
        edge_count,
        edges,
    }
}
