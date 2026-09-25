---
name: system_info
description: Display system information and resource usage
command: "echo '=== System Info ===' && hostname && uname -a && echo '=== Memory ===' && free -h && echo '=== Disk ===' && df -h"
parameters: ""
---

# System Information

Get a quick overview of your system's current state.

**Usage:**
No parameters required.

**Shows:**
- System hostname and OS details
- Memory usage and availability
- Disk space across all mounted filesystems

Perfect for:
- Quick system health checks
- Debugging resource issues
- Documenting system state
