# CLAUDE.md

## Project context

AWS pipeline that finds AI startup funding news, researches each company
with grounded web search and emails a detailed PDF report.

- `orchestrator/`: Lambda in ap-south-1 (Mumbai). Entry point
  `lambda_function.lambda_handler`. `pdf_report.py` renders the PDF.
- `research/`: Lambda in us-west-2 (Oregon). Two steps: Nova with
  `nova_grounding` writes prose notes (real citations captured), then an
  ungrounded call formats them into JSON citing sources by ID.
- `prompts/`: copies of the S3 prompts the orchestrator reads at run time.

## Rules

- Never remove existing features when changing code. Extend instead.
- The research response contract (`research_packet`, `sources`,
  `research_meta` in `body`) is shared by both Lambdas; change both sides
  together.
- Source URLs in reports must come from grounding citations, never from
  model-typed text.
- Keep the amount raised and the valuation as separate fields.
- Python 3.14 runtime. The orchestrator ships `markdown` and `reportlab`
  (which needs Pillow, so builds are architecture-specific); the research
  Lambda uses only boto3.
- Run `python3 tests/test_pipeline.py` after changes; deploy with
  `scripts/deploy.sh`; prompts with `scripts/push_prompts.sh`.
- Do not commit `config.env`, zips or `dist/`.
- Ask before running anything that deploys or pushes.
