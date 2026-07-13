# RAG Evaluation

This directory is reserved for evaluating the coal-regulation RAG pipeline.

Planned layout:

- `data/`: labeled or semi-labeled evaluation cases.
- `scripts/`: retrieval and RAGAS evaluation scripts.
- `reports/`: generated metric reports.

## Checkpoint And Resume

### Human annotation app

- Every label, checkbox, and note edit is immediately saved to browser `localStorage`.
- Closing and reopening the same HTML in the same browser resumes the saved state.
- Use `导出标注 JSON` regularly to create a portable checkpoint.
- Use `导入检查点` to resume from an exported JSON after changing browsers, clearing browser data, or regenerating the HTML.

### LLM pre-annotation

`scripts/preannotate_gold_cases_with_llm.py` writes progress incrementally to:

```text
data/gold_preannotations_llm_v9.json
```

The output stores:

- `cases`: completed two-pass annotations.
- `drafts`: first-pass annotations waiting for review.
- `failures`: the failed case, stage, error, and timestamp.

The file is updated after both the draft and completed stages using an atomic temporary-file replacement. Rerunning the same command skips completed cases and resumes saved drafts from the review stage.
