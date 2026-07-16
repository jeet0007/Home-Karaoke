"""Renders a QR code straight to an inline SVG string.

Deliberately bypasses the `qrcode` package's image-backend factories (which
pull in Pillow for PNG, or optionally pypng) - `QRCode.get_matrix()` already
hands back the plain boolean module grid, which is enough to draw a crisp
vector image ourselves with zero extra dependencies. See the comment on
`qrcode` in requirements.txt for why that matters here.
"""

import qrcode

# Modules per side is data-dependent (QR "version"); this is the on-screen
# pixel size of the square SVG viewport, not the module count.
DEFAULT_SIZE_PX = 320


def qr_svg(data, size_px=DEFAULT_SIZE_PX, border_modules=2):
    """Returns a self-contained SVG document (as a str) encoding `data`."""
    qr = qrcode.QRCode(border=0)
    qr.add_data(data)
    qr.make(fit=True)
    matrix = qr.get_matrix()

    modules_per_side = len(matrix)
    total_modules = modules_per_side + border_modules * 2
    module_px = size_px / total_modules

    rects = []
    for row_index, row in enumerate(matrix):
        for col_index, is_dark in enumerate(row):
            if not is_dark:
                continue
            x = (col_index + border_modules) * module_px
            y = (row_index + border_modules) * module_px
            rects.append(f'<rect x="{x:.3f}" y="{y:.3f}" width="{module_px:.3f}" height="{module_px:.3f}"/>')

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size_px} {size_px}" '
        f'width="{size_px}" height="{size_px}" shape-rendering="crispEdges">'
        f'<rect width="{size_px}" height="{size_px}" fill="#fff"/>'
        f'<g fill="#000">{"".join(rects)}</g>'
        f"</svg>"
    )
