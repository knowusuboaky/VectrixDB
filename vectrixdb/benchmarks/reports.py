"""
Benchmark Reports

Generate reports in various formats from benchmark results.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any
from .runner import BenchmarkResult


class BenchmarkReport:
    """
    Generate benchmark reports in various formats.

    Supports Markdown, JSON, HTML, and console output.

    Example:
        >>> report = BenchmarkReport(results)
        >>> print(report.to_markdown())
        >>> report.save("benchmark_report.md")
    """

    def __init__(
        self,
        results: List[BenchmarkResult],
        title: str = "VectrixDB Benchmark Report",
        description: str = "",
    ):
        """
        Initialize benchmark report.

        Args:
            results: List of benchmark results
            title: Report title
            description: Report description
        """
        self.results = results
        self.title = title
        self.description = description
        self.timestamp = datetime.now()

    def to_markdown(self) -> str:
        """
        Generate Markdown report.

        Returns:
            Markdown formatted string
        """
        lines = [
            f"# {self.title}",
            "",
            f"*Generated: {self.timestamp.strftime('%Y-%m-%d %H:%M:%S')}*",
            "",
        ]

        if self.description:
            lines.extend([self.description, ""])

        # Summary table
        lines.extend([
            "## Summary",
            "",
            "| Benchmark | Ops/sec | Latency P50 | Latency P99 | Memory Peak |",
            "|-----------|---------|-------------|-------------|-------------|",
        ])

        for result in self.results:
            lines.append(
                f"| {result.name} | "
                f"{result.operations_per_second:,.0f} | "
                f"{result.latency_p50_ms:.2f}ms | "
                f"{result.latency_p99_ms:.2f}ms | "
                f"{result.memory_peak_mb:.1f}MB |"
            )

        lines.append("")

        # Detailed results
        lines.extend(["## Detailed Results", ""])

        for result in self.results:
            lines.extend([
                f"### {result.name}",
                "",
                f"- **Duration:** {result.duration_ms:.2f}ms",
                f"- **Operations/sec:** {result.operations_per_second:,.2f}",
                f"- **Throughput items:** {result.throughput_items:,}",
                "",
                "**Latency:**",
                f"- Mean: {result.latency_mean_ms:.3f}ms",
                f"- Std: {result.latency_std_ms:.3f}ms",
                f"- P50: {result.latency_p50_ms:.3f}ms",
                f"- P95: {result.latency_p95_ms:.3f}ms",
                f"- P99: {result.latency_p99_ms:.3f}ms",
                "",
                "**Memory:**",
                f"- Peak: {result.memory_peak_mb:.2f}MB",
                f"- Delta: {result.memory_delta_mb:.2f}MB",
                "",
            ])

            if result.recall_at_k is not None:
                lines.append(f"**Recall@k:** {result.recall_at_k:.4f}")
                lines.append("")

            if result.custom_metrics:
                lines.append("**Custom Metrics:**")
                for key, value in result.custom_metrics.items():
                    if isinstance(value, float):
                        lines.append(f"- {key}: {value:.4f}")
                    else:
                        lines.append(f"- {key}: {value}")
                lines.append("")

        return "\n".join(lines)

    def to_json(self, indent: int = 2) -> str:
        """
        Generate JSON report.

        Args:
            indent: JSON indentation

        Returns:
            JSON formatted string
        """
        data = {
            "title": self.title,
            "description": self.description,
            "timestamp": self.timestamp.isoformat(),
            "results": [r.to_dict() for r in self.results],
        }
        return json.dumps(data, indent=indent)

    def to_html(self) -> str:
        """
        Generate HTML report.

        Returns:
            HTML formatted string
        """
        html = f"""
<!DOCTYPE html>
<html>
<head>
    <title>{self.title}</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 40px; background: #f5f5f5; }}
        h1 {{ color: #333; }}
        h2 {{ color: #666; border-bottom: 2px solid #ddd; padding-bottom: 10px; }}
        table {{ border-collapse: collapse; width: 100%; background: white; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
        th, td {{ border: 1px solid #ddd; padding: 12px; text-align: left; }}
        th {{ background: #6366f1; color: white; }}
        tr:nth-child(even) {{ background: #f9f9f9; }}
        tr:hover {{ background: #f0f0f0; }}
        .metric {{ display: inline-block; background: #e8e8e8; padding: 4px 8px; margin: 2px; border-radius: 4px; }}
        .good {{ color: #10b981; }}
        .warning {{ color: #f59e0b; }}
        .card {{ background: white; padding: 20px; margin: 20px 0; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
        .timestamp {{ color: #999; font-size: 14px; }}
    </style>
</head>
<body>
    <h1>{self.title}</h1>
    <p class="timestamp">Generated: {self.timestamp.strftime('%Y-%m-%d %H:%M:%S')}</p>
    {"<p>" + self.description + "</p>" if self.description else ""}

    <h2>Summary</h2>
    <table>
        <tr>
            <th>Benchmark</th>
            <th>Ops/sec</th>
            <th>Latency P50</th>
            <th>Latency P99</th>
            <th>Memory Peak</th>
        </tr>
"""

        for result in self.results:
            html += f"""
        <tr>
            <td>{result.name}</td>
            <td>{result.operations_per_second:,.0f}</td>
            <td>{result.latency_p50_ms:.2f}ms</td>
            <td>{result.latency_p99_ms:.2f}ms</td>
            <td>{result.memory_peak_mb:.1f}MB</td>
        </tr>
"""

        html += """
    </table>

    <h2>Detailed Results</h2>
"""

        for result in self.results:
            html += f"""
    <div class="card">
        <h3>{result.name}</h3>
        <p>
            <span class="metric">Duration: {result.duration_ms:.2f}ms</span>
            <span class="metric">Throughput: {result.operations_per_second:,.0f} ops/sec</span>
            <span class="metric">Items: {result.throughput_items:,}</span>
        </p>
        <p>
            <strong>Latency:</strong>
            <span class="metric">Mean: {result.latency_mean_ms:.3f}ms</span>
            <span class="metric">P50: {result.latency_p50_ms:.3f}ms</span>
            <span class="metric">P95: {result.latency_p95_ms:.3f}ms</span>
            <span class="metric">P99: {result.latency_p99_ms:.3f}ms</span>
        </p>
        <p>
            <strong>Memory:</strong>
            <span class="metric">Peak: {result.memory_peak_mb:.2f}MB</span>
            <span class="metric">Delta: {result.memory_delta_mb:.2f}MB</span>
        </p>
"""
            if result.recall_at_k is not None:
                html += f"""
        <p><strong>Recall@k:</strong> <span class="metric good">{result.recall_at_k:.4f}</span></p>
"""
            html += """
    </div>
"""

        html += """
</body>
</html>
"""
        return html

    def to_console(self) -> str:
        """
        Generate console-friendly output.

        Returns:
            Formatted string for terminal
        """
        lines = [
            "=" * 60,
            f" {self.title}",
            "=" * 60,
            "",
        ]

        # Summary
        lines.append("SUMMARY")
        lines.append("-" * 60)
        lines.append(f"{'Benchmark':<25} {'Ops/sec':>12} {'P50':>10} {'P99':>10}")
        lines.append("-" * 60)

        for result in self.results:
            lines.append(
                f"{result.name:<25} "
                f"{result.operations_per_second:>12,.0f} "
                f"{result.latency_p50_ms:>9.2f}ms "
                f"{result.latency_p99_ms:>9.2f}ms"
            )

        lines.extend(["", "=" * 60])

        return "\n".join(lines)

    def save(
        self,
        path: Path,
        format: str = "auto",
    ) -> None:
        """
        Save report to file.

        Args:
            path: Output file path
            format: Format (auto, markdown, json, html)
        """
        path = Path(path)

        if format == "auto":
            suffix = path.suffix.lower()
            format_map = {
                ".md": "markdown",
                ".json": "json",
                ".html": "html",
                ".txt": "console",
            }
            format = format_map.get(suffix, "markdown")

        if format == "markdown":
            content = self.to_markdown()
        elif format == "json":
            content = self.to_json()
        elif format == "html":
            content = self.to_html()
        else:
            content = self.to_console()

        path.write_text(content, encoding="utf-8")


def compare_results(
    baseline: List[BenchmarkResult],
    current: List[BenchmarkResult],
) -> Dict[str, Dict[str, Any]]:
    """
    Compare two sets of benchmark results.

    Args:
        baseline: Baseline results
        current: Current results to compare

    Returns:
        Comparison data with improvements/regressions
    """
    baseline_map = {r.name: r for r in baseline}
    current_map = {r.name: r for r in current}

    all_names = set(baseline_map.keys()) | set(current_map.keys())
    comparisons = {}

    for name in all_names:
        base = baseline_map.get(name)
        curr = current_map.get(name)

        if base and curr:
            # Calculate improvements
            ops_change = ((curr.operations_per_second - base.operations_per_second) /
                         base.operations_per_second * 100) if base.operations_per_second else 0

            latency_change = ((base.latency_p50_ms - curr.latency_p50_ms) /
                             base.latency_p50_ms * 100) if base.latency_p50_ms else 0

            memory_change = ((base.memory_peak_mb - curr.memory_peak_mb) /
                            base.memory_peak_mb * 100) if base.memory_peak_mb else 0

            comparisons[name] = {
                "baseline_ops": base.operations_per_second,
                "current_ops": curr.operations_per_second,
                "ops_change_percent": ops_change,
                "baseline_latency_p50": base.latency_p50_ms,
                "current_latency_p50": curr.latency_p50_ms,
                "latency_change_percent": latency_change,
                "baseline_memory": base.memory_peak_mb,
                "current_memory": curr.memory_peak_mb,
                "memory_change_percent": memory_change,
                "improved": ops_change > 5 or latency_change > 5,
                "regressed": ops_change < -5 or latency_change < -5,
            }
        elif base:
            comparisons[name] = {"status": "removed"}
        else:
            comparisons[name] = {"status": "new"}

    return comparisons
