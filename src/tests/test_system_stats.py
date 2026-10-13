"""Unit tests for system_stats text parsers and payload helpers."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import system_stats


class ParseNvidiaSmiTests(unittest.TestCase):
    def test_parses_rows_and_handles_unavailable(self):
        text = (
            "0, NVIDIA GeForce RTX 4090, 61, 45, 8192, 24576\n"
            "1, NVIDIA GeForce RTX 3060, [N/A], 12, 2048, 12288\n"
        )
        gpus = system_stats.parse_nvidia_smi(text)
        self.assertEqual(len(gpus), 2)
        self.assertEqual(gpus[0]["name"], "NVIDIA GeForce RTX 4090")
        self.assertEqual(gpus[0]["temp"], 61.0)
        self.assertEqual(gpus[0]["util"], 45.0)
        self.assertEqual(gpus[0]["mem_total"], 24576 * 1024 * 1024)
        self.assertIsNone(gpus[1]["temp"])
        self.assertEqual(gpus[1]["util"], 12.0)

    def test_name_with_comma(self):
        gpus = system_stats.parse_nvidia_smi("0, Weird, Name GPU, 50, 10, 100, 200")
        self.assertEqual(gpus[0]["name"], "Weird, Name GPU")
        self.assertEqual(gpus[0]["temp"], 50.0)

    def test_ignores_junk(self):
        self.assertEqual(system_stats.parse_nvidia_smi("not,a,valid,line"), [])


class ParseDfTests(unittest.TestCase):
    HEADER = "Filesystem 1024-blocks Used Available Capacity Mounted on\n"

    def test_filters_pseudo_filesystems(self):
        text = self.HEADER + (
            "/dev/sda1 10000000 5000000 5000000 50% /\n"
            "tmpfs 100 50 50 50% /run\n"
            "map auto_home 0 0 0 100% /home\n"
            "devfs 186 0 186 100% /dev\n"
        )
        disks = system_stats.parse_df(text)
        self.assertEqual(len(disks), 1)
        self.assertEqual(disks[0]["mount"], "/")
        self.assertEqual(disks[0]["percent"], 50.0)

    def test_dedupes_same_usage_signature(self):
        text = self.HEADER + (
            "/dev/disk3s1s1 10000000 4000000 6000000 40% /\n"
            "/dev/disk3s5 10000000 4000000 6000000 40% /System/Volumes/Data\n"
            "/dev/disk4 20000000 10000000 10000000 50% /Volumes/Backup\n"
        )
        disks = system_stats.parse_df(text)
        self.assertEqual(len(disks), 2)
        self.assertEqual(disks[0]["mount"], "/")
        self.assertEqual(disks[1]["mount"], "/Volumes/Backup")

    def test_keeps_overlay(self):
        text = self.HEADER + "overlay 10000000 7000000 3000000 70% /var/lib/docker/overlay/x\n"
        disks = system_stats.parse_df(text)
        self.assertEqual(len(disks), 1)
        self.assertEqual(disks[0]["device"], "overlay")

    def test_skips_tiny_filesystems(self):
        text = self.HEADER + "/dev/sdb1 100 50 50 50% /boot/efi\n"
        self.assertEqual(system_stats.parse_df(text), [])

    def test_groups_apfs_containers(self):
        text = self.HEADER + (
            "/dev/disk3s1s1 10000000 4000000 6000000 40% /\n"
            "/dev/disk3s5 10000000 8000000 2000000 80% /System/Volumes/Data\n"
            "/dev/disk4s1 20000000 10000000 10000000 50% /Volumes/Backup\n"
        )
        disks = system_stats.parse_df(text, group_containers=True)
        self.assertEqual(len(disks), 2)
        self.assertEqual(disks[0]["mount"], "/System/Volumes/Data")
        self.assertEqual(disks[0]["percent"], 80.0)


class ParseWmicDisksTests(unittest.TestCase):
    def test_parses_value_output(self):
        text = "\n\nCaption=C:\nFreeSpace=500000000000\nSize=1000000000000\n\n"
        disks = system_stats.parse_wmic_disks(text)
        self.assertEqual(len(disks), 1)
        self.assertEqual(disks[0]["mount"], "C:\\")
        self.assertEqual(disks[0]["percent"], 50.0)

    def test_skips_small_partitions(self):
        text = "\n\nCaption=D:\nFreeSpace=100\nSize=1000\n\n"
        self.assertEqual(system_stats.parse_wmic_disks(text), [])


class MemPayloadTests(unittest.TestCase):
    def test_percent_and_swap(self):
        data = system_stats._mem_payload(10, 5, 4, 1)
        self.assertEqual(data["percent"], 50.0)
        self.assertEqual(data["swap_percent"], 25.0)

    def test_no_swap(self):
        data = system_stats._mem_payload(10, 5, 0, 0)
        self.assertNotIn("swap_total", data)


if __name__ == "__main__":
    unittest.main()
