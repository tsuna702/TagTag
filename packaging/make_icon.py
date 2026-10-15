"""앱 아이콘(LocalTag.icns · Windows 용 LocalTag.ico) 그리기: 둥근 사각형 위에 태그 모양 + 음표."""
import os
import subprocess
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath, QPen


def draw(size: int) -> QImage:
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    s = size / 1024
    # 배경 (macOS 아이콘 격자: 824px 사각형, 여백 100px)
    bg = QRectF(100 * s, 100 * s, 824 * s, 824 * s)
    g = QLinearGradient(bg.topLeft(), bg.bottomRight())
    g.setColorAt(0, QColor("#ff7a59"))
    g.setColorAt(1, QColor("#e5376b"))
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(g))
    p.drawRoundedRect(bg, 185 * s, 185 * s)
    # 태그 모양
    tag = QPainterPath()
    tag.moveTo(300 * s, 330 * s)
    tag.lineTo(600 * s, 330 * s)
    tag.lineTo(760 * s, 512 * s)
    tag.lineTo(600 * s, 694 * s)
    tag.lineTo(300 * s, 694 * s)
    tag.closeSubpath()
    p.setBrush(QColor(255, 255, 255, 235))
    p.drawPath(tag)
    p.setBrush(QColor("#e5376b"))
    p.drawEllipse(QPointF(615 * s, 512 * s), 30 * s, 30 * s)
    # 음표
    p.setPen(QPen(QColor("#3a2340"), 34 * s, Qt.SolidLine, Qt.RoundCap))
    p.drawLine(QPointF(470 * s, 400 * s), QPointF(470 * s, 600 * s))
    p.setPen(QPen(QColor("#3a2340"), 30 * s, Qt.SolidLine, Qt.RoundCap))
    p.drawLine(QPointF(470 * s, 400 * s), QPointF(535 * s, 440 * s))
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#3a2340"))
    p.save()
    p.translate(425 * s, 605 * s)
    p.rotate(-20)
    p.drawEllipse(QRectF(-58 * s, -40 * s, 116 * s, 80 * s))
    p.restore()
    p.end()
    return img


def write_ico(out_ico, sizes=(16, 24, 32, 48, 64, 128, 256)):
    """Windows 아이콘: 크기별 PNG 를 담은 .ico (Vista 이후 형식)."""
    import struct
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    pngs = []
    for size in sizes:
        data = QByteArray()
        buf = QBuffer(data)
        buf.open(QIODevice.WriteOnly)
        draw(size).save(buf, "PNG")
        pngs.append((size, bytes(data)))
    head = struct.pack("<HHH", 0, 1, len(pngs))
    offset = 6 + 16 * len(pngs)
    entries, body = b"", b""
    for size, png in pngs:
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset + len(body))
        body += png
    with open(out_ico, "wb") as f:
        f.write(head + entries + body)


def main(out_icns):
    app = QGuiApplication(sys.argv)
    if sys.platform == "win32":  # iconutil 이 없으므로 .ico 만
        write_ico(out_icns.replace(".icns", ".ico"))
        return
    iconset = out_icns.replace(".icns", ".iconset")
    os.makedirs(iconset, exist_ok=True)
    for base in (16, 32, 128, 256, 512):
        draw(base).save(os.path.join(iconset, f"icon_{base}x{base}.png"))
        draw(base * 2).save(os.path.join(iconset, f"icon_{base}x{base}@2x.png"))
    subprocess.run(["iconutil", "-c", "icns", iconset, "-o", out_icns], check=True)
    draw(512).save(out_icns.replace(".icns", ".png"))
    write_ico(out_icns.replace(".icns", ".ico"))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "LocalTag.icns"))
