---
name: backup_creator
description: Create a timestamped backup of a directory
command: "tar -czf {backup_name}_$(date +%Y%m%d_%H%M%S).tar.gz {source_path}"
parameters: "source_path: string, backup_name: string"
---

# Backup Creator

Create compressed backups with automatic timestamps.

**Usage:**
- `source_path`: Directory or file to backup (required)
- `backup_name`: Base name for the backup file (required)

**Creates:**
- Compressed tar.gz archive
- Automatic timestamp in filename (YYYYMMDD_HHMMSS)
- Preserves file permissions and structure

Useful for:
- Before making risky changes
- Regular backups of important data
- Archiving old projects
