# Analysis Notebook Guide

## Overview

The analysis notebook (`analysis.ipynb`) provides data exploration, model performance visualization, and presentation materials for the Genomic Variant Interpretation System. It complements the production system by offering interactive visualizations and statistical analysis tools.

## Purpose

The analysis notebook serves two distinct purposes:

1. **Data Exploration and Research**: Statistical analysis, model training experiments, and information retrieval demonstrations
2. **Presentation Materials**: High-quality visualizations for assignment presentations, technical reports, and demonstrations

This approach maintains separation between the production system (designed for deployment) and research tools (designed for exploration and presentation).

## Analysis Notebook Structure

The notebook contains 12 sections covering the complete analysis pipeline:

1. **Setup & Dependencies** - Package installation and environment configuration
2. **Load Dataset** - Dataset loading and initial inspection
3. **Data Exploration & Statistical Analysis** - Distribution analysis and statistical summaries
4. **Feature Engineering for Model Analysis** - Feature preparation and encoding
5. **Model Training & Evaluation** - Random Forest training and performance metrics
6. **Integration with Production System Evaluation** - Connection to production evaluation script
7. **Classification Performance Visualization** - Per-class metrics and performance analysis
8. **Information Retrieval Performance Visualization** - IR metrics and retrieval effectiveness
9. **TF-IDF Information Retrieval Demonstration** - IR system component demonstration
10. **System Architecture Overview** - Visual system architecture diagram
11. **Summary & Presentation Materials** - Comprehensive summary for presentations
12. **Save All Results** - Automated visualization generation and saving

## Generated Visualizations

Running the analysis notebook generates 11 professional visualizations in the `analysis_results/` directory:

### Data Exploration Visualizations
- `gene_distribution.png` - Distribution of variants across BRCA1/BRCA2 genes and expert agreement status
- `variant_types.png` - Top 10 variant types in the dataset
- `impact_levels.png` - Variant impact levels (HIGH, MODERATE, LOW, etc.)
- `allele_frequencies.png` - Population allele frequency distributions across ESP, ExAC, and TGP databases
- `cadd_scores.png` - CADD score distribution with pathogenic threshold (20) highlighted

### Model Performance Visualizations
- `confusion_matrix.png` - Confusion matrix for expert conflict prediction with heatmap
- `roc_curve.png` - ROC curve analysis with AUC score
- `feature_importance.png` - Feature importance ranking showing what drives expert disagreement
- `classification_performance.png` - Per-class precision, recall, and F1 scores

### System Performance Visualizations
- `ir_performance.png` - Information retrieval performance metrics (Precision@5, Recall@5, Exact-match@5)
- `system_architecture.png` - Visual diagram of the 4-agent system architecture

## Usage Instructions

### Quick Start
```bash
./run_analysis.sh
```

### Manual Setup
```bash
# Install additional dependencies if needed
pip install matplotlib seaborn jupyter

# Create results directory
mkdir -p analysis_results

# Start Jupyter notebook
jupyter notebook analysis.ipynb
```

### Running the Notebook
1. Open the notebook in Jupyter or VS Code
2. Run all cells (Cell → Run All)
3. Visualizations will be automatically saved to `analysis_results/`
4. Use the generated visualizations for presentations and analysis

## System Integration

### Production System vs. Analysis Notebook

**Production System** (`main.py`, `api.py`, `evaluation.py`):
- **Purpose**: Deployment, API service, production use
- **Output**: Text-based metrics, JSON responses, console output
- **Design**: Server-ready, no GUI dependencies, automated monitoring
- **Use Case**: Production deployment, API integration, automated testing

**Analysis Notebook** (`analysis.ipynb`):
- **Purpose**: Research, exploration, presentation materials
- **Output**: Visual graphs, statistical analysis, presentation materials
- **Design**: Interactive, exploration-focused, visual output
- **Use Case**: Data exploration, model development, presentation creation

### Integration Points
1. **Shared Dataset**: Both use the same ClinVar dataset (`data/clinvar_variant_dataset.xlsx`)
2. **Consistent Algorithms**: Both implement identical classification and IR logic
3. **Production Evaluation**: Notebook integrates with `evaluation.py` for metric consistency
4. **Complementary Results**: Visualizations explain and illustrate text-based production metrics

### Missingness and Metric Interpretation

The dataset contains variant-dependent missing annotations. The production
pipeline preserves these values as missing, records predictor availability, and
does not treat an absent SIFT, PolyPhen, or CADD value as zero evidence. The
classifier skips rules that require unavailable predictors and lowers confidence
when important predictors are missing.

When interpreting evaluation results, compare overall accuracy with the
complete and incomplete predictor subgroups. A complete record has finite SIFT,
PolyPhen, and CADD values; an incomplete record is missing at least one of
these predictors. The inferred ground truth remains a simplified evidence-based
proxy and should not be presented as official clinical validation.

## Project Structure

```
variant_agent_system/
├── analysis.ipynb               # Analysis notebook
├── analysis_results/            # Generated visualizations
│   ├── README.md               # Visualization usage guide
│   ├── gene_distribution.png
│   ├── variant_types.png
│   ├── impact_levels.png
│   ├── allele_frequencies.png
│   ├── cadd_scores.png
│   ├── confusion_matrix.png
│   ├── roc_curve.png
│   ├── feature_importance.png
│   ├── classification_performance.png
│   ├── ir_performance.png
│   └── system_architecture.png
├── run_analysis.sh             # Quick start script
├── agents/                     # Production agents
├── data/                       # Dataset
├── frontend/                   # Web UI
├── llm/                        # LLM client
├── protocol/                   # Communication protocol
├── tests/                      # Unit tests
├── api.py                      # REST API
├── evaluation.py               # Evaluation script
├── main.py                     # CLI orchestrator
└── requirements.txt            # Dependencies
```

## Dependencies

The analysis notebook requires additional dependencies beyond the production system:

```bash
pip install matplotlib>=3.7.0
pip install seaborn>=0.12.0
pip install jupyter>=1.0.0
```

These are included in the updated `requirements.txt` but are optional for production deployment.

## Troubleshooting

### Missing Dependencies
```bash
pip install matplotlib seaborn jupyter
```

### Dataset Not Found
Ensure the dataset is located at `data/clinvar_variant_dataset.xlsx`

### Jupyter Installation Issues
```bash
# Install Jupyter
pip install jupyter

# Alternative: Use VS Code's built-in Jupyter support
# Open analysis.ipynb directly in VS Code
```

### Visualization Saving Issues
- Verify that `analysis_results/` directory exists and is writable
- Check file permissions for the project directory
- Ensure sufficient disk space for generated images

## Customization

The notebook can be customized for specific needs:

- **Color Schemes**: Modify matplotlib/seaborn color parameters
- **Figure Sizes**: Adjust `plt.rcParams['figure.figsize']` values
- **Additional Analyses**: Add new cells for custom visualizations
- **Export Formats**: Change save formats from PNG to PDF, SVG, etc.

## Best Practices

1. **Run Before Presentations**: Generate fresh visualizations before each presentation
2. **Version Control**: Commit the notebook but exclude generated images from git
3. **Documentation**: Update this guide when adding new visualizations
4. **Backup**: Keep copies of key visualizations for presentations
5. **Consistency**: Ensure notebook algorithms match production system logic