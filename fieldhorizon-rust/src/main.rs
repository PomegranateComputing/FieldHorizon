mod io;
mod ontology;
mod pressure;
mod types;

use std::path::PathBuf;

use anyhow::Result;
use clap::Parser;

use crate::io::read_entries;
use crate::ontology::read_ontology;
use crate::pressure::build_pressure_report;

#[derive(Debug, Parser)]
#[command(name = "fh-core")]
#[command(about = "Field Horizon Rust Core v0.1")]
struct Args {
    #[arg(long)]
    input: PathBuf,

    #[arg(long, default_value_t = 50)]
    limit: usize,

    /// Path to ontology.yaml -- the single source of truth for opposition
    /// pairs, shared with the Python side (fieldhorizon.ontology_spec) so
    /// the two pressure engines can never disagree.
    #[arg(long, default_value = "ontology.yaml")]
    ontology: PathBuf,
}

fn main() -> Result<()> {
    let args = Args::parse();

    let entries = read_entries(&args.input)?;
    let oppositions = read_ontology(&args.ontology)?;
    let report = build_pressure_report(&entries, args.limit, &oppositions);

    println!("{}", serde_json::to_string_pretty(&report)?);

    Ok(())
}
