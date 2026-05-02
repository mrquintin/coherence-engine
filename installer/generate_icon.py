"""Generate a macOS .icns app icon for the Coherence Engine.

Creates a multi-layered icon with the overlapping circles motif representing
coherence measurement. Outputs to the installer directory.
"""

import struct
import zlib
import os
import math

OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))


def create_png(width, height, pixels):
    """Create a minimal PNG file from raw RGBA pixel data."""
    def chunk(chunk_type, data):
        c = chunk_type + data
        crc = struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
        return struct.pack(">I", len(data)) + c + crc

    header = b'\x89PNG\r\n\x1a\n'
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)

    raw_rows = b""
    for y in range(height):
        raw_rows += b'\x00'
        for x in range(width):
            idx = (y * width + x) * 4
            raw_rows += bytes(pixels[idx:idx+4])

    compressed = zlib.compress(raw_rows, 9)

    return header + chunk(b'IHDR', ihdr) + chunk(b'IDAT', compressed) + chunk(b'IEND', b'')


def draw_icon(size):
    """Draw the Coherence Engine icon at the given size."""
    pixels = [0] * (size * size * 4)
    cx, cy = size / 2, size / 2
    bg_r, bg_g, bg_b = 30, 30, 46
    ring_r, ring_g, ring_b = 137, 180, 250
    center_r, center_g, center_b = 166, 227, 161

    for y in range(size):
        for x in range(size):
            idx = (y * size + x) * 4

            dx = x - cx
            dy = y - cy
            dist = math.sqrt(dx * dx + dy * dy)

            # Background: dark circle with soft edge
            bg_radius = size * 0.46
            if dist <= bg_radius:
                alpha = 255
                if dist > bg_radius - 2:
                    alpha = int(255 * (bg_radius - dist) / 2)
                    alpha = max(0, min(255, alpha))
                pixels[idx] = bg_r
                pixels[idx + 1] = bg_g
                pixels[idx + 2] = bg_b
                pixels[idx + 3] = alpha
                continue

            pixels[idx + 3] = 0
            continue

    # Draw three overlapping rings (coherence motif)
    offsets = [
        (-size * 0.1, -size * 0.08),
        (size * 0.1, -size * 0.08),
        (0, size * 0.08),
    ]

    ring_radius = size * 0.2
    ring_width = max(2, size * 0.04)

    for ox, oy in offsets:
        rcx = cx + ox
        rcy = cy + oy

        for y in range(size):
            for x in range(size):
                idx = (y * size + x) * 4
                dx = x - rcx
                dy = y - rcy
                dist = math.sqrt(dx * dx + dy * dy)

                edge_dist = abs(dist - ring_radius)
                if edge_dist < ring_width:
                    alpha = int(255 * (1 - edge_dist / ring_width))
                    alpha = max(0, min(255, alpha))

                    old_a = pixels[idx + 3]
                    if alpha > 0:
                        blend = alpha / 255.0
                        old_blend = old_a / 255.0

                        new_a = 1 - (1 - blend) * (1 - old_blend)
                        if new_a > 0:
                            pixels[idx] = int((ring_r * blend + pixels[idx] * old_blend * (1 - blend)) / new_a)
                            pixels[idx + 1] = int((ring_g * blend + pixels[idx + 1] * old_blend * (1 - blend)) / new_a)
                            pixels[idx + 2] = int((ring_b * blend + pixels[idx + 2] * old_blend * (1 - blend)) / new_a)
                            pixels[idx + 3] = int(new_a * 255)

    # Draw center dot (convergence point)
    dot_radius = size * 0.05
    for y in range(size):
        for x in range(size):
            idx = (y * size + x) * 4
            dx = x - cx
            dy = y - cy
            dist = math.sqrt(dx * dx + dy * dy)

            if dist < dot_radius:
                alpha = 255
                if dist > dot_radius - 1.5:
                    alpha = int(255 * (dot_radius - dist) / 1.5)

                blend = alpha / 255.0
                old_a = pixels[idx + 3] / 255.0
                new_a = 1 - (1 - blend) * (1 - old_a)
                if new_a > 0:
                    pixels[idx] = int((center_r * blend + pixels[idx] * old_a * (1 - blend)) / new_a)
                    pixels[idx + 1] = int((center_g * blend + pixels[idx + 1] * old_a * (1 - blend)) / new_a)
                    pixels[idx + 2] = int((center_b * blend + pixels[idx + 2] * old_a * (1 - blend)) / new_a)
                    pixels[idx + 3] = int(new_a * 255)

    return pixels


def create_icns(output_path):
    """Create a macOS .icns file with multiple resolutions."""
    # icns type codes for PNG-based icons
    icon_types = [
        (b'ic07', 128),
        (b'ic08', 256),
        (b'ic09', 512),
        (b'ic11', 32),
        (b'ic12', 64),
        (b'ic13', 256),
        (b'ic14', 512),
    ]

    entries = []
    seen_sizes = set()

    for type_code, size in icon_types:
        if size in seen_sizes:
            continue
        seen_sizes.add(size)

        pixels = draw_icon(size)
        png_data = create_png(size, size, pixels)
        entries.append((type_code, png_data))

    # Build icns file
    body = b''
    for type_code, png_data in entries:
        entry_size = 8 + len(png_data)
        body += type_code + struct.pack(">I", entry_size) + png_data

    total_size = 8 + len(body)
    icns_data = b'icns' + struct.pack(">I", total_size) + body

    with open(output_path, 'wb') as f:
        f.write(icns_data)

    print(f"Created icon: {output_path} ({len(icns_data)} bytes)")


if __name__ == "__main__":
    output = os.path.join(OUTPUT_DIR, "CoherenceEngine.icns")
    create_icns(output)
