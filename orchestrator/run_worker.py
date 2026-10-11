import asyncio
import logging
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from temporalio.client import Client
from temporalio.worker import Worker

from orchestrator.workflow import PBIDevelopmentWorkflow, ComponentPipelineWorkflow
from orchestrator.activities import (
    generate_example_map_activity,
    decompose_specs_activity,
    draft_schema_activity,
    initialize_tasks_activity,
    run_design_activity,
    run_tdd_cycle_activity,
    integration_and_pr_activity,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("orchestrator.worker")

TASK_QUEUE = os.getenv("TEMPORAL_TASK_QUEUE", "ai-dev-task-queue")

async def main():
    temporal_address = os.getenv("TEMPORAL_ADDRESS", "localhost:7233")
    logger.info(f"Connecting to Temporal Server at {temporal_address}...")
    
    try:
        client = await Client.connect(temporal_address)
        logger.info("Successfully connected to Temporal!")
    except Exception as e:
        logger.error(f"Failed to connect to Temporal: {e}")
        logger.error("Please ensure Temporal Server is running (e.g. docker compose up -d)")
        sys.exit(1)

    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[PBIDevelopmentWorkflow, ComponentPipelineWorkflow],
        activities=[
            generate_example_map_activity,
            decompose_specs_activity,
            draft_schema_activity,
            initialize_tasks_activity,
            run_design_activity,
            run_tdd_cycle_activity,
            integration_and_pr_activity,
        ],
    )

    logger.info(f"Worker is starting on queue '{TASK_QUEUE}'... Press Ctrl+C to stop.")
    await worker.run()

if __name__ == "__main__":
    asyncio.run(main())
