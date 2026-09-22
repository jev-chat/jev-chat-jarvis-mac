"""Read-only visual fallback. A rectangle is never an editable AX target."""
import ctypes
import Quartz


def input_outline(image):
    """Find the long separator above WeChat's message composer.

    Older WeChat builds draw a closed rectangle around the composer. WeChat 4.x draws
    only its top separator and left divider because the composer ends at the window
    bottom. Accept either shape, but only let an open-bottom separator qualify when it
    reaches the right window edge. Coordinates are normalized top-origin; no fixed
    sidebar width or input height is assumed. Ambiguous frames return None.
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
    tops = [(right-left, y, left, right) for y,left,right in rows
            if y < h*.90 and right-left > w*.40]
    if not tops:
        return None
    _, y, left, right = max(tops)
    bottoms = [(by, bl, br) for by,bl,br in rows
               if by > max(y+20,h*.90) and abs(bl-left)<12 and abs(br-right)<12]
    if not bottoms:
        # WeChat 4.x has no lower stroke: the input panel simply continues to the
        # bottom of the window. Requiring the detected separator to touch the right
        # edge keeps message bubbles and other internal rules from becoming targets.
        if right < w-12 or h-y < max(20,h*.08):
            return None
        by, bl, br = h-1, left, w-1
    else:
        by, bl, br = max(bottoms)
    return (min(left,bl)/w, y/h, (max(right,br)-min(left,bl))/w, (by-y)/h)


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
