---
name: port_checker
description: Check if a network port is available or in use
command: "netstat -an | grep {port} || echo 'Port {port} is available'"
parameters: "port: string"
---

# Port Checker

Verify if a network port is available or already in use.

**Usage:**
- `port`: Port number to check (required, e.g., "8080")

**Output:**
- Shows any processes using the port
- Reports "Port is available" if nothing is listening

Helpful for:
- Debugging connection issues
- Finding available ports for new services
- Verifying services are running
