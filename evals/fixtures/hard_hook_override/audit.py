"""Report on what a pipeline processed."""


def summarise(pipeline, items):
    results = pipeline.run(items)
    return {"processed": len(pipeline.applied), "results": results}
