"""CLI for Curator service using Click."""

import asyncio
import click
from rich.console import Console
from rich.table import Table
from rich import print as rprint
import uvicorn

from curator.config import get_settings
from curator.storage import CuratorStorage
from curator.orchestrator import IngestionOrchestrator
from curator.models import SubscriptionType

console = Console()


@click.group()
def main():
    """Curator - Content acquisition and curation service."""
    pass


@main.command()
@click.option("--host", default=None, help="Host to bind to")
@click.option("--port", default=None, type=int, help="Port to bind to")
@click.option("--reload", is_flag=True, help="Enable auto-reload")
def serve(host, port, reload):
    """Start the Curator API server."""
    settings = get_settings()

    # Use CLI args or fall back to settings
    api_host = host or settings.api_host
    api_port = port or settings.api_port

    console.print(f"[bold green]Starting Curator API server on {api_host}:{api_port}[/bold green]")

    uvicorn.run(
        "curator.api:app",
        host=api_host,
        port=api_port,
        reload=reload,
        log_level="info",
    )


@main.command()
@click.argument("url")
@click.option("--subscription-id", type=int, help="Optional subscription ID")
def ingest(url, subscription_id):
    """Ingest a single URL."""
    settings = get_settings()
    storage = CuratorStorage()
    orchestrator = IngestionOrchestrator(storage, settings)

    console.print(f"[bold]Ingesting:[/bold] {url}")

    async def _ingest():
        success = await orchestrator.ingest_url(url, subscription_id=subscription_id)
        return success

    success = asyncio.run(_ingest())

    if success:
        console.print("[bold green]✓ Ingestion completed successfully[/bold green]")
    else:
        console.print("[bold red]✗ Ingestion failed[/bold red]")
        exit(1)


@main.group()
def subscription():
    """Manage subscriptions."""
    pass


@subscription.command("list")
def list_subscriptions():
    """List all subscriptions."""
    storage = CuratorStorage()
    subscriptions = storage.list_subscriptions()

    if not subscriptions:
        console.print("[yellow]No subscriptions found[/yellow]")
        return

    table = Table(title="Subscriptions")
    table.add_column("ID", style="cyan")
    table.add_column("Name", style="green")
    table.add_column("Type", style="blue")
    table.add_column("URL", style="white")
    table.add_column("Status", style="yellow")
    table.add_column("Enabled", style="magenta")

    for sub in subscriptions:
        table.add_row(
            str(sub["id"]),
            sub["name"],
            sub["subscription_type"],
            sub["source_url"][:50] + "..." if len(sub["source_url"]) > 50 else sub["source_url"],
            sub["status"],
            "✓" if sub["enabled"] else "✗",
        )

    console.print(table)


@subscription.command("add")
@click.argument("name")
@click.argument("url")
@click.option("--type", "sub_type", type=click.Choice(["youtube_channel", "rss_feed", "podcast"]), default="youtube_channel")
@click.option("--frequency", type=int, default=60, help="Check frequency in minutes")
def add_subscription(name, url, sub_type, frequency):
    """Add a new subscription."""
    storage = CuratorStorage()

    try:
        sub_id = storage.create_subscription(
            name=name,
            subscription_type=SubscriptionType(sub_type),
            source_url=url,
            check_frequency_minutes=frequency,
        )

        console.print(f"[bold green]✓ Subscription created with ID: {sub_id}[/bold green]")

    except Exception as e:
        console.print(f"[bold red]✗ Failed to create subscription: {e}[/bold red]")
        exit(1)


@main.command()
@click.argument("sub_type", type=click.Choice(["youtube_channel", "rss_feed", "podcast"]))
@click.argument("url")
@click.option("--name", help="Subscription name (defaults to URL)")
@click.option("--frequency", type=int, default=60, help="Check frequency in minutes")
def subscribe(sub_type, url, name, frequency):
    """Add a new subscription (shorthand for subscription add)."""
    storage = CuratorStorage()

    # Use URL as name if not provided
    subscription_name = name or url

    try:
        sub_id = storage.create_subscription(
            name=subscription_name,
            subscription_type=SubscriptionType(sub_type),
            source_url=url,
            check_frequency_minutes=frequency,
        )

        console.print(f"[bold green]✓ Subscription created with ID: {sub_id}[/bold green]")

    except Exception as e:
        console.print(f"[bold red]✗ Failed to create subscription: {e}[/bold red]")
        exit(1)


@subscription.command("remove")
@click.argument("subscription_id", type=int)
def remove_subscription(subscription_id):
    """Remove a subscription."""
    storage = CuratorStorage()

    success = storage.delete_subscription(subscription_id)

    if success:
        console.print(f"[bold green]✓ Subscription {subscription_id} deleted[/bold green]")
    else:
        console.print(f"[bold red]✗ Subscription {subscription_id} not found[/bold red]")
        exit(1)


@subscription.command("enable")
@click.argument("subscription_id", type=int)
def enable_subscription(subscription_id):
    """Enable a subscription."""
    storage = CuratorStorage()

    success = storage.update_subscription(subscription_id, enabled=True)

    if success:
        console.print(f"[bold green]✓ Subscription {subscription_id} enabled[/bold green]")
    else:
        console.print(f"[bold red]✗ Subscription {subscription_id} not found[/bold red]")
        exit(1)


@subscription.command("disable")
@click.argument("subscription_id", type=int)
def disable_subscription(subscription_id):
    """Disable a subscription."""
    storage = CuratorStorage()

    success = storage.update_subscription(subscription_id, enabled=False)

    if success:
        console.print(f"[bold green]✓ Subscription {subscription_id} disabled[/bold green]")
    else:
        console.print(f"[bold red]✗ Subscription {subscription_id} not found[/bold red]")
        exit(1)


@main.command()
@click.option("--limit", type=int, default=20, help="Number of items to show")
def items(limit):
    """List ingested items."""
    storage = CuratorStorage()
    items = storage.list_ingested_items(limit=limit)

    if not items:
        console.print("[yellow]No items found[/yellow]")
        return

    table = Table(title="Ingested Items")
    table.add_column("ID", style="cyan")
    table.add_column("Title", style="green")
    table.add_column("Author", style="blue")
    table.add_column("Type", style="white")
    table.add_column("Status", style="yellow")
    table.add_column("Chunks", style="magenta")

    for item in items:
        table.add_row(
            str(item["id"]),
            item["title"][:40] + "..." if len(item["title"]) > 40 else item["title"],
            item["author"] or "Unknown",
            item["source_type"],
            item["status"],
            str(item["chunk_count"]),
        )

    console.print(table)


@main.command("visual-context")
@click.argument("video_id")
@click.argument("timestamp", type=float)
@click.option("--prewarm", is_flag=True, help="Pre-warm VLM model before querying")
def visual_context(video_id, timestamp, prewarm):
    """Get visual context for a YouTube video frame (via glimpse service)."""
    import httpx

    settings = get_settings()
    glimpse_url = settings.glimpse_service_url

    async def _run():
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=330.0, write=10.0, pool=10.0)
        ) as client:
            if prewarm:
                console.print("[dim]Pre-warming VLM model...[/dim]")
                await client.post(f"{glimpse_url}/v1/prewarm")

            console.print(f"[bold]Extracting visual context:[/bold] {video_id} @ {timestamp}s")
            r = await client.post(
                f"{glimpse_url}/v1/glimpse",
                json={"video_id": video_id, "timestamp_sec": timestamp},
            )
            r.raise_for_status()
            return r.json()

    try:
        result = asyncio.run(_run())
    except httpx.ConnectError:
        console.print("[red]Error: Glimpse service unavailable[/red]")
        raise SystemExit(1)

    # Display results
    if result.get("error"):
        console.print(f"[red]Error: {result['error']}[/red]")

    if result.get("caption") or result.get("ocr_text"):
        console.print(f"\n[bold green]Caption:[/bold green] {result.get('caption', '')}")
        if result.get("ocr_text"):
            console.print(f"\n[bold blue]OCR Text:[/bold blue]\n{result['ocr_text']}")
        if result.get("entity_types"):
            console.print(f"\n[bold yellow]Entity Types:[/bold yellow] {', '.join(result['entity_types'])}")
    elif not result.get("vlm_available"):
        console.print("[yellow]No VLM backend available. Frame saved for manual inspection.[/yellow]")
        if result.get("frame_path"):
            console.print(f"[dim]Frame: {result['frame_path']}[/dim]")

    # Latency breakdown
    lat_parts = [f"Frame: {result['frame_latency_ms']}ms"]
    if result.get("vlm_latency_ms") is not None:
        lat_parts.append(f"VLM: {result['vlm_latency_ms']}ms")
    lat_parts.append(f"Total: {result['latency_ms']}ms")
    console.print(f"\n[dim]{' | '.join(lat_parts)}[/dim]")


@main.command()
def daemon():
    """Run the subscription monitoring daemon."""
    from curator.daemon import SubscriptionDaemon

    settings = get_settings()
    # Extract database path from URL (sqlite:///path/to/db.db -> path/to/db.db)
    db_path = settings.database_url.replace("sqlite:///", "")
    storage = CuratorStorage(database_path=db_path)

    console.print("[bold green]Starting Curator daemon...[/bold green]")

    daemon = SubscriptionDaemon(storage, settings)
    daemon.run()


if __name__ == "__main__":
    main()
