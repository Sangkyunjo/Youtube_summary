def prioritize_channels(items, channel_id, config):
    """Move configured channels to the front, keeping the order within each group."""
    priority_ids = set((config.get('youtube') or {}).get('priority_channel_ids') or ())
    return sorted(items, key=lambda item: channel_id(item) not in priority_ids)
