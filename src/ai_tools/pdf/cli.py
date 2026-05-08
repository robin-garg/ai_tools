"""CLI commands for PDF generation.

Commands
--------
ai-tools pdf save   – Markdown → PDF  (structured reports, headings, tables)
ai-tools pdf text   – Plain text → PDF (logs, configs, terminal output; monospace)
ai-tools pdf rtf    – RTF → PDF        (preserves bold/italic via textutil)

Examples::

    ai-tools pdf save  -i report.md      -o report.pdf
    ai-tools pdf save  -i report.md      -o report_edt.pdf --tz EDT
    ai-tools pdf text  -i server.log     -o server_log.pdf
    ai-tools pdf text  -i nginx.conf     -o nginx_conf.pdf --title "Nginx Config"
    ai-tools pdf rtf   -i document.rtf   -o document.pdf
    cat report.md | ai-tools pdf save    -o report.pdf
    cat access.log | ai-tools pdf text   -o access_log.pdf
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from ai_tools.pdf.writer import KNOWN_TIMEZONES, markdown_to_pdf, text_to_pdf, rtf_to_pdf


@click.group()
def pdf() -> None:
    """Convert text/markdown documents to PDF."""


@pdf.command()
@click.option(
    "--input", "-i",
    "input_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Source markdown file. Reads from stdin if omitted.",
)
@click.option(
    "--output", "-o",
    "output_path",
    type=click.Path(dir_okay=False, path_type=Path),
    required=True,
    help="Destination PDF file path.",
)
@click.option(
    "--title", "-t",
    default="",
    show_default=False,
    help="Document title (shown in PDF metadata and footer).",
)
@click.option(
    "--tz",
    "tz_label",
    default="UTC",
    show_default=True,
    type=click.Choice(sorted(KNOWN_TIMEZONES), case_sensitive=False),
    help="Timezone label for the document header/footer.",
)
@click.option(
    "--convert-tz/--no-convert-tz",
    default=True,
    show_default=True,
    help=(
        "Convert UTC HH:MM:SS timestamps in the document to --tz before rendering. "
        "Use --no-convert-tz to keep times as-is and only change the label."
    ),
)
def save(
    input_path: Path | None,
    output_path: Path,
    title: str,
    tz_label: str,
    convert_tz: bool,
) -> None:
    """Convert a markdown file (or stdin) to a PDF.

    Examples:\n
        ai-tools pdf save -i analysis.md -o report_utc.pdf --tz UTC\n
        ai-tools pdf save -i analysis.md -o report_edt.pdf --tz EDT\n
        cat analysis.md | ai-tools pdf save -o report.pdf --tz UTC
    """
    if input_path is not None:
        content = input_path.read_text(encoding="utf-8")
        if not title:
            title = input_path.stem.replace("_", " ").title()
    else:
        if sys.stdin.isatty():
            click.echo("No --input file given and stdin is a terminal. Pipe markdown text or use --input.", err=True)
            raise SystemExit(2)
        content = sys.stdin.read()
        if not title:
            title = output_path.stem.replace("_", " ").title()

    convert_target = tz_label if convert_tz else None

    click.echo(f"Rendering PDF → {output_path}  [timezone: {tz_label}]", err=True)
    result = markdown_to_pdf(
        content=content,
        output_path=output_path,
        title=title,
        tz_label=tz_label.upper(),
        convert_from_utc_to=convert_target,
    )
    click.echo(f"Saved: {result}", err=True)



@pdf.command()
@click.option(
    "--input", "-i",
    "input_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Source plain-text file. Reads from stdin if omitted.",
)
@click.option(
    "--output", "-o",
    "output_path",
    type=click.Path(dir_okay=False, path_type=Path),
    required=True,
    help="Destination PDF file path.",
)
@click.option(
    "--title", "-t",
    default="",
    show_default=False,
    help="Document title (shown in PDF metadata and footer).",
)
@click.option(
    "--tz",
    "tz_label",
    default="UTC",
    show_default=True,
    type=click.Choice(sorted(KNOWN_TIMEZONES), case_sensitive=False),
    help="Timezone label for the document footer (display only, no conversion).",
)
def text(
    input_path: Path | None,
    output_path: Path,
    title: str,
    tz_label: str,
) -> None:
    """Convert a plain-text file (or stdin) to a PDF.

    Renders in Courier monospace, preserving every space, tab, and newline.
    Ideal for log files, config files, terminal output, and unformatted notes.

    Examples:\n
        ai-tools pdf text -i server.log -o server_log.pdf\n
        ai-tools pdf text -i nginx.conf -o nginx_conf.pdf --title "Nginx Config"\n
        cat access.log | ai-tools pdf text -o access_log.pdf
    """
    if input_path is not None:
        content = input_path.read_text(encoding="utf-8")
        if not title:
            title = input_path.stem.replace("_", " ").title()
    else:
        if sys.stdin.isatty():
            click.echo(
                "No --input file given and stdin is a terminal. "
                "Pipe text or use --input.",
                err=True,
            )
            raise SystemExit(2)
        content = sys.stdin.read()
        if not title:
            title = output_path.stem.replace("_", " ").title()

    click.echo(f"Rendering PDF → {output_path}  [timezone: {tz_label}]", err=True)
    result = text_to_pdf(
        content=content,
        output_path=output_path,
        title=title,
        tz_label=tz_label.upper(),
    )
    click.echo(f"Saved: {result}", err=True)


@pdf.command()
@click.option(
    "--input", "-i",
    "input_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Source RTF file.",
)
@click.option(
    "--output", "-o",
    "output_path",
    type=click.Path(dir_okay=False, path_type=Path),
    required=True,
    help="Destination PDF file path.",
)
@click.option(
    "--title", "-t",
    default="",
    show_default=False,
    help="Document title (shown in PDF metadata and footer).",
)
@click.option(
    "--tz",
    "tz_label",
    default="UTC",
    show_default=True,
    type=click.Choice(sorted(KNOWN_TIMEZONES), case_sensitive=False),
    help="Timezone label for the document footer (display only, no conversion).",
)
def rtf(
    input_path: Path,
    output_path: Path,
    title: str,
    tz_label: str,
) -> None:
    """Convert an RTF file to a PDF, preserving bold, italic, and tables.

    Uses macOS ``textutil`` to convert the RTF to HTML internally, then
    renders it with ReportLab.  Requires macOS (textutil is a system tool).

    Examples:\n
        ai-tools pdf rtf -i document.rtf -o document.pdf\n
        ai-tools pdf rtf -i notes.rtf    -o notes.pdf --title "Meeting Notes"
    """
    if not title:
        title = input_path.stem.replace("_", " ").title()

    click.echo(f"Rendering PDF → {output_path}  [timezone: {tz_label}]", err=True)
    result = rtf_to_pdf(
        rtf_path=input_path,
        output_path=output_path,
        title=title,
        tz_label=tz_label.upper(),
    )
    click.echo(f"Saved: {result}", err=True)
