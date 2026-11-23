"""SpaceBio Satellite: a command-line client that lends a little of your computer to SpaceBio.

It asks the SpaceBio core for a small task (a keyword, a database, a window of dates), runs it against the public scientific
databases from YOUR computer, and sends the paper records back. The core never trusts it: it verifies a sample of every batch.
The contract between the two is ``docs/CONTRACT.md``.
"""

__version__ = "0.1.0"
API_MAJOR = "1"                    # the contract's major version this client speaks (``/api/satellite/v1/``)
TASK_KINDS = ("harvest/1",)        # what this client can run; sent to the core as ``capabilities``
RECORD_SCHEMAS = ("paper/1",)
