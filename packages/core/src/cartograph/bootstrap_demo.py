"""Create the demo schema + WAL slot + DDL trigger on the configured cluster.

    python -m cartograph.bootstrap_demo

Thin wrapper over Cartograph.bootstrap()."""

from .core import Cartograph

if __name__ == "__main__":
    Cartograph.bootstrap()
    print("bootstrapped: customers/orders/line_items (REPLICA IDENTITY FULL), "
          "logical slot 'cg', DDL event trigger.")
