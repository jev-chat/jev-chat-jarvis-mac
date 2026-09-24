"""Read-only visual fallback. A rectangle is never an editable AX target."""
import ctypes
import Quartz


def input_outline(image):
    """Find the long separator above WeChat's message composer.

    Older WeChat builds draw a closed rectangle around the composer. WeChat 4.x draws
    only its top separator and left divider because the composer ends at the window
    bottom. Accept either shape only when its remaining edges confirm the separator;
    a long horizontal rule alone is not enough. Coordinates are normalized top-origin;
    no fixed sidebar width or input height is assumed. Ambiguous frames return None.
    """
    width, height = Quartz.CGImageGetWidth(image), Quartz.CGImageGetHeight(image)
    w, h = 640, round(height * 640 / width)
    if h < 80:
        return None
    buf = ctypes.create_string_buffer(w*h*4)
    ctx = Quartz.CGBitmapContextCreate(buf, w, h, 8, w*4,
        Quartz.CGColorSpaceCreateDeviceRGB(), Quartz.kCGImageAlphaPremultipliedLast)
    if ctx is None:
        return None
    Quartz.CGContextDrawImage(ctx, Quartz.CGRectMake(0, 0, w, h), image)
    data = buf.raw
    def gray(x, y):
        i = (y*w+x)*4
        return sum(data[i:i+3])/3
    def run(y):
        best = (0, 0)
        start = None
        last = -1
        for x in range(5, w-5):
            if abs(gray(x,y)-gray(x,y+1)) >= 2:
                if start is None or x-last > 4:
                    start=x
                last=x
                if x-start > best[1]-best[0]:
                    best=(start,x)
        return best
    rows = [(y, *run(y)) for y in range(int(h*.50), h-2)]
    def divider_reaches_bottom(left, top):
        # A 4.x composer may have no bottom border and its top rule may stop short
        # of the window edge. Its *left* divider must then remain visible right down
        # to the bottom. A quoted card or message bubble ends much earlier.
        if h-top < max(20, h*.08):
            return False
        samples = range(top+5, h-4, max(4, (h-top)//18))
        hits = 0
        count = 0
        bottom_hit = False
        for yy in samples:
            count += 1
            edge = any(abs(gray(x,yy)-gray(x+1,yy)) >= 2
                       for x in range(max(1,left-5), min(w-2,left+5)))
            hits += edge
            if yy >= h-max(12, h*.08) and edge:
                bottom_hit = True
        return count > 0 and hits >= count*.7 and bottom_hit

    # A long message bubble or quoted-card rule can also span 40% of the window.
    # Validate an actual lower border or a divider reaching the window bottom before
    # allowing a candidate to crop the chat OCR region. Prefer the lowest valid top.
    tops = [(y,left,right) for y,left,right in rows
            if y < h*.90 and right-left > w*.40 and left < w*.65 and right > w*.78]
    for y, left, right in sorted(tops, reverse=True):
        bottoms = [(by, bl, br) for by,bl,br in rows
                   if by > max(y+20,h*.90) and abs(bl-left)<12 and abs(br-right)<12]
        if bottoms:
            by, bl, br = max(bottoms)
        elif divider_reaches_bottom(left, y):
            if h-y < max(20,h*.08):
                continue
            by, bl, br = h-1, left, w-1
        else:
            continue
        return (min(left,bl)/w, y/h, (max(right,br)-min(left,bl))/w, (by-y)/h)
    return None


def locate_visual_input(win):
    from perception import capture_image
    try:
        image = capture_image(win['wid'])
        if image is None:
            return None
        rect = input_outline(image)
    except Exception:
        return None
    if rect is None:
        return None
    x, y, w, h = rect
    return (win['x']+x*win['w'], win['y']+y*win['h'], w*win['w'], h*win['h'])
