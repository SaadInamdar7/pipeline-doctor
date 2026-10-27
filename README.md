# pipeline-doctor

Root-cause diagnosis for failures in AWS Glue, Amazon MWAA (Apache Airflow), Amazon Athena and AWS Lake Formation.

Given an error message, logs and a few configuration facts, or the ID of a live Glue job run, MWAA environment or Athena query, pipeline-doctor returns the most likely root cause, the evidence behind it, fix steps and a link to the relevant AWS documentation. It runs as a command-line tool or as an MCP server for use from coding agents.

## Background

Error text in data pipelines often points at a symptom rather than the cause. A few common examples:

- A Glue job that times out connecting to S3 or a JDBC database may have no S3 gateway endpoint in its subnet, or a security group without a self-referencing inbound rule. The error message is the same in both cases.
- A crawler that fails with a Lake Formation `AccessDeniedException` usually needs a data location permission, not a table permission.
- An Airflow task reported as a zombie was often killed by the operating system for using too much memory.

pipeline-doctor encodes 31 such failure patterns and uses configuration facts, and optionally an LLM, to tell look-alike failures apart.

## How it works

1. **Rule engine.** Each pattern in [`patterns.yaml`](src/pipeline_doctor/patterns.yaml) has error signatures (regular expressions) and optional configuration checks such as `no_s3_route`, `sg_missing_self_reference` or `workers_capped`. Signature matches add to a pattern's score; configuration checks add to or subtract from it. The result is a ranked list of candidates.
2. **LLM step (optional, `--llm`).** Claude on Amazon Bedrock receives the incident, the ranked candidates and the full pattern catalog, and returns a JSON-schema-constrained answer. The answer must be a known pattern ID or `unknown`, so the model cannot invent a cause. If the primary model declines a request, the SDK's refusal-fallback middleware retries it on a fallback model.
3. **Report.** The chosen pattern supplies the root-cause description, ordered fix steps and documentation link.

Live mode uses read-only AWS API calls (`Get*` and `Describe*`) to collect the error, logs and network facts for a resource.

## Evaluation

[`evals/scenarios.yaml`](evals/scenarios.yaml) contains 43 synthetic incidents: one straightforward scenario per pattern, plus 12 harder ones. The hard scenarios cover generic errors that need configuration to resolve, incidents with no service given, noisy logs, symptoms reported instead of causes, and plain-language descriptions with no error string. All identifiers in the scenarios are fictional.

Rule engine results:

| Set | Scenarios | Top-1 | Top-3 |
|---|---|---|---|
| Easy | 31 | 100% | 100% |
| Hard | 12 | 58.3% | 83.3% |
| Overall | 43 | 88.4% | 95.3% |

Median latency is under 1 ms per incident. The rules were not tuned to the hard set; the five misses are the cases the LLM step is meant to handle. Full results are in [`evals/results-rules.json`](evals/results-rules.json).

To reproduce, or to score the LLM step (requires Bedrock model access):

```bash
uv run pipeline-doctor eval --out evals/results-rules.json
uv run pipeline-doctor --llm eval --out evals/results-llm.json
```

## Installation

Requires Python 3.12 or later and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/SaadInamdar7/pipeline-doctor.git
cd pipeline-doctor
uv sync
uv run pytest
```

The tests make no AWS calls.

## Usage

Diagnose an incident from JSON (the format matches the `incident` field in `evals/scenarios.yaml`):

```bash
echo '{"service": "athena", "error": "HIVE_TOO_MANY_OPEN_PARTITIONS: Exceeded limit of 100 open writers"}' \
  | uv run pipeline-doctor diagnose -
```

Diagnose live resources with your current AWS credentials:

```bash
uv run pipeline-doctor glue <job-name> <run-id>
uv run pipeline-doctor mwaa <environment-name> --error "tasks stuck in queued state"
uv run pipeline-doctor athena <query-execution-id>
```

Add `--llm` to any command to include the model's reasoning, and `--json` for machine-readable output.

### MCP server

```json
{
  "mcpServers": {
    "pipeline-doctor": {
      "command": "uv",
      "args": ["--directory", "/path/to/pipeline-doctor", "run", "pipeline-doctor-mcp"]
    }
  }
}
```

The server exposes `diagnose_incident`, `diagnose_glue_job_run`, `diagnose_mwaa_environment`, `diagnose_athena_query` and `list_failure_patterns`. It communicates over stdio and does not open a network port.

### Configuration

| Environment variable | Default |
|---|---|
| `PIPELINE_DOCTOR_MODEL` | `anthropic.claude-opus-5-5` |
| `PIPELINE_DOCTOR_FALLBACK_MODEL` | `anthropic.claude-opus-4-8` |
| `AWS_REGION` | `us-east-1` |

### IAM permissions

Live mode needs `glue:GetJobRun`, `glue:GetJob`, `glue:GetConnection`, `ec2:DescribeRouteTables`, `ec2:DescribeSubnets`, `ec2:DescribePrefixLists`, `ec2:DescribeSecurityGroups`, `logs:GetLogEvents`, `airflow:GetEnvironment` and `athena:GetQueryExecution`. The `--llm` option also needs `bedrock:InvokeModel`.

## Adding a pattern

1. Add an entry to `src/pipeline_doctor/patterns.yaml`.
2. Add at least one scenario for it to `evals/scenarios.yaml`. The test suite fails if a pattern has no scenario.
3. If the pattern needs a new configuration check, add it to `CHECKS` in `src/pipeline_doctor/engine.py`.

## License

MIT
