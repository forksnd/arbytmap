import os
import time
import mmap

from array import array
from copy import deepcopy
from math import ceil, log
from struct import pack_into, unpack_from, unpack
from traceback import format_exc
tga_def = dds_def = png_def = None

from arbytmap.constants import *
try:
    from supyr_struct.defs.bitmaps.dds import dds_def
    from supyr_struct.defs.bitmaps.tga import tga_def
    from supyr_struct.defs.bitmaps.objs.png import pad_idat_data
    from supyr_struct.defs.bitmaps.png import png_def
except Exception:
    print("SupyrStruct was not loaded. Cannot load or save non-raw images.")

try:
    from arbytmap.ext import bitmap_io_ext
    fast_bitmap_io = True
except:
    fast_bitmap_io = False


#this will be the reference to the bitmap convertor module.
#once the module loads this will become the reference to it.
ab = None


def get_channel_order_by_masks(a_mask=0, r_mask=0, g_mask=0, b_mask=0):
    # shift the masks right until they're all the same scale
    a_shift = r_shift = g_shift = b_shift = 0
    while a_mask and not(a_mask&1):
        a_mask = a_mask >> 1
        a_shift += 1

    while r_mask and not(r_mask&1):
        r_mask = r_mask >> 1
        r_shift += 1

    while g_mask and not(g_mask&1):
        g_mask = g_mask >> 1
        g_shift += 1

    while b_mask and not(b_mask&1):
        b_mask = b_mask >> 1
        b_shift += 1

    src_order_map = {}
    if a_mask: src_order_map[a_shift] = 'a'
    if r_mask: src_order_map[r_shift] = 'r'
    if g_mask: src_order_map[g_shift] = 'g'
    if b_mask: src_order_map[b_shift] = 'b'
    return "".join(src_order_map[k] for k in sorted(src_order_map))


def get_dds_channel_map(a_mask=0, r_mask=0, g_mask=0, b_mask=0,
                        dst_order=C_ORDER_BGRA):
    # NOTE: This is extremely confusing and carefully calibrated
    # to work with ARGB and RGB channels. DO NOT TOUCH
    src_order = get_channel_order_by_masks(a_mask, r_mask, g_mask, b_mask)
    channel_map = get_channel_swap_mapping(src_order, dst_order)
    new_order = "".join(("bgra"[i] if i in range(4) else "x")
                        for i in channel_map[::-1])
    return get_channel_swap_mapping(C_ORDER_ARGB, new_order)


def get_channel_swap_mapping(src_order, dst_order=C_ORDER_ARGB):
    '''Takes a source channel order string and a destination channel
    order string and returns a 4 item tuple that maps the source
    channels to the destination order.
    Valid src channel strings contain any arrangement of 'argb'.
    The same appliesfor dst channel strings, but 'x' can also be used
    to signify a channel is to be made blank(dropped from the source).
    '''
    src_order = src_order.lower()
    dst_order = dst_order.lower()
    assert set(src_order).issubset("argb"), (
        "Source channel order must contain only the characters 'argbARGB'. "
        "'%s' is an invalid channel order." % src_order
        )
    assert set(dst_order).issubset("argbx"), (
        "Source channel order must contain only the characters 'argbxARGBX'. "
        "'%s' is an invalid channel order." % dst_order
        )
    channel_map = []
    for c in dst_order:
        if c in src_order:
            channel_map.append(src_order.index(c))
        else:
            channel_map.append(-1)

    return tuple(channel_map)


def load_from_dds_file(convertor, input_path, ext, **kwargs):
    """Loads a DDS file into the convertor."""
    dds_file = dds_def.build(filepath="%s.%s" % (input_path, ext))

    try:
        head = dds_file.data.header
        fmt_head  = head.dds_pixelformat
        fmt_flags = fmt_head.flags
        err = ""

        if fmt_head.four_cc.enum_name == "DX10":
            err += "CANNOT LOAD DX10 DDS FILES.\n"

        if head.caps2.volume and head.caps2.cubemap:
            err += ("ERROR: DDS HEADER INVALID. TEXTURE " +
                    "SPECIFIED AS BOTH CUBEMAP AND VOLUMETRIC.\n")

        mipmap_count = max(head.mipmap_count - 1, 0)
        typ = ab.TYPE_2D
        sub_bitmap_count = 1
        if head.caps2.volume:
            typ = ab.TYPE_3D
        elif head.caps2.cubemap:
            typ = ab.TYPE_CUBEMAP
            sub_bitmap_count = sum(bool(head.caps2[n]) for name in
                                   ("pos_x", "pos_y", "pos_z",
                                    "neg_x", "neg_y", "neg_z"))

        fmt = None
        bitdepths = set()
        for mask in (fmt_head.r_bitmask, fmt_head.g_bitmask,
                     fmt_head.b_bitmask, fmt_head.a_bitmask):
            bitdepths.add(sum((mask>>i)&1 for i in range(32)))

        if fmt_flags.four_cc:
            # the texture has a compression method
            fmt = fmt_head.four_cc.enum_name
            if fmt == "CxV8U8":
                fmt = ab.FORMAT_V8U8
            elif fmt.startswith("LIN_"):
                fmt = fmt.lstrip("LIN_")

            if   fmt == ab.FORMAT_DXT3A:
                if   fmt_flags.alpha_only: pass
                elif fmt_flags.has_alpha:  fmt = ab.FORMAT_DXT3AY
                elif fmt_flags.luminance:  fmt = ab.FORMAT_DXT3Y
            elif fmt == ab.FORMAT_DXT5A:
                if   fmt_flags.alpha_only: pass
                elif fmt_flags.has_alpha:  fmt = ab.FORMAT_DXT5AY
                elif fmt_flags.luminance:  fmt = ab.FORMAT_DXT5Y

            if fmt not in ab.VALID_FORMATS:
                fmt = None
        elif fmt_flags.rgb_space:
            if fmt_head.rgb_bitcount == 8:
                if   bitdepths == set((0, 2, 3)): fmt = ab.FORMAT_R3G3B2
            elif fmt_head.rgb_bitcount in (15, 16):
                if   bitdepths == set((2, 3, 8)): fmt = ab.FORMAT_A8R3G3B2
                elif bitdepths == set((0, 5, 6)): fmt = ab.FORMAT_R5G6B5
                elif bitdepths == set((1, 5)):    fmt = ab.FORMAT_A1R5G5B5
                elif bitdepths == set((0, 5)):    fmt = ab.FORMAT_A1R5G5B5
                elif bitdepths == set((4,  )):    fmt = ab.FORMAT_A4R4G4B4
            elif fmt_head.rgb_bitcount == 24:  fmt = ab.FORMAT_R8G8B8
            elif fmt_head.rgb_bitcount == 32:
                if   bitdepths == set((0, 8)): fmt = ab.FORMAT_X8R8G8B8
                elif bitdepths == set((8,  )): fmt = ab.FORMAT_A8R8G8B8
        elif fmt_flags.alpha_only:
            if   fmt_head.rgb_bitcount == 8:  fmt = ab.FORMAT_A8
            elif fmt_head.rgb_bitcount == 16: fmt = ab.FORMAT_A16
        elif fmt_flags.has_alpha:
            if   fmt_head.rgb_bitcount == 16: fmt = ab.FORMAT_A8L8
            elif fmt_head.rgb_bitcount == 32: fmt = ab.FORMAT_A16L16
        elif fmt_flags.luminance:
            if   fmt_head.rgb_bitcount == 8:  fmt = ab.FORMAT_L8
            elif fmt_head.rgb_bitcount == 16: fmt = ab.FORMAT_L16
        elif fmt_flags.vu_space:
            if   fmt_head.rgb_bitcount == 16: fmt = ab.FORMAT_V8U8
            elif fmt_head.rgb_bitcount == 32: fmt = ab.FORMAT_V16U16
        elif fmt_flags.yuv_space:  fmt = ab.FORMAT_Y8U8V8

        if fmt is None:
            err += "UNABLE TO DETERMINE DDS FORMAT. FAILED TO LOAD TEXTURE.\n"

        if err:
            print(err)
            return

        chan_ct = ab.CHANNEL_COUNTS[fmt]
        channel_map = None
        if not fmt_flags.four_cc and chan_ct > 2:
            # swap channels so everything is ARGB
            channel_map = get_dds_channel_map(
                fmt_head.a_bitmask, fmt_head.r_bitmask,
                fmt_head.g_bitmask, fmt_head.b_bitmask,
                C_ORDER_BGRA)[: chan_ct]


        tex_info = {"width": head.width, "height": head.height,
                    "depth": max(head.depth, 1), "texture_type": typ,
                    "filepath": dds_file.filepath, "format": fmt,
                    "mipmap_count": mipmap_count,
                    "sub_bitmap_count": sub_bitmap_count}

        temp = []
        #loop over each mipmap and cube face
        #and turn them into pixel arrays
        dds_data, off = dds_file.data.pixel_data, 0
        for sb in range(sub_bitmap_count):
            for m in range(mipmap_count + 1):
                dims = ab.get_mipmap_dimensions(
                    head.width, head.height, head.depth, m)
                off = bitmap_bytes_to_array(dds_data, off, temp, fmt, *dims)

        # rearrange the images so they are sorted by [mip][bitmap]
        tex_block = [None]*len(temp)
        for m in range(mipmap_count + 1):
            for sb in range(sub_bitmap_count):
                tex_block[m*sub_bitmap_count + sb] = temp[
                    sb*(mipmap_count + 1) + m]

        convertor.load_new_texture(texture_block=tex_block,
                                   texture_info=tex_info)

        if channel_map:
            convertor.load_new_conversion_settings(
                channel_mapping=channel_map)
            convertor.convert_texture()
    except:
        print(format_exc())


def load_from_tga_file(convertor, input_path, ext, **kwargs):
    """Loads a TGA file into the convertor."""
    tga_file = tga_def.build(filepath="%s.%s" % (input_path, ext))

    head = tga_file.data.header
    image_desc = head.image_descriptor
    pixels  = tga_file.data.pixels_wrapper.pixels
    palette = tga_file.data.color_table
    cm_bpp = head.color_map_depth
    bpp    = head.bpp
    alpha_depth = image_desc.alpha_bit_count
    if cm_bpp == 15: cm_bpp, alpha_depth = 16, 1
    if    bpp == 15: bpp,    alpha_depth = 16, 1

    #do another check to make sure image is color mapped
    color_mapped = head.image_type.format.enum_name == "color_mapped_rgb"

    tex_info = {"width":head.width, "height":head.height, "depth":1,
                "texture_type":"2D", "mipmap_count":0,
                "sub_bitmap_count":1, "filepath":input_path}

    err = ""
    #figure out what color format we've got
    bpp_test = cm_bpp if color_mapped else bpp

    fmt_name = head.image_type.format.enum_name
    fmt = None
    if bpp_test == 1 or fmt_name == "bw_1_bit":
        fmt = ab.FORMAT_A1
        err += "Unable to load black and white 1-bit color Targa images."
    elif bpp_test == 8:
        if fmt_name == "unmapped_rgb": pass
        elif alpha_depth == 0: fmt = ab.FORMAT_L8
        elif alpha_depth == 4: fmt = ab.FORMAT_A4L4
        elif alpha_depth == 8: fmt = ab.FORMAT_A8
    elif bpp_test == 16:
        if   alpha_depth == 8: fmt = ab.FORMAT_A8L8
        elif alpha_depth == 1: fmt = ab.FORMAT_A1R5G5B5
    elif bpp_test == 24:       fmt = ab.FORMAT_R8G8B8
    elif bpp_test == 32:
        if   alpha_depth == 0: fmt = ab.FORMAT_X8R8G8B8
        elif alpha_depth == 8: fmt = ab.FORMAT_A8R8G8B8

    if fmt not in ab.VALID_FORMATS:
        err += ("Unable to load %sbit color Targa images." % bpp)

    tex_info["format"] = fmt
    if image_desc.interleaving.data:
        err += "Unable to load Targa images with interleaved pixels."

    if err:
        print(err)
        return

    tex_block = []
    if image_desc.screen_origin.enum_name == "lower_left":
        tga_file.flip_image_origin()
        pixels = tga_file.data.pixels_wrapper.pixels

    typecode = ab.PACKED_TYPECODES[tex_info["format"]]
    if color_mapped:
        if cm_bpp == 24:
            palette = pad_24bit_array(palette)
        elif cm_bpp == 48:
            palette = pad_48bit_array(palette)
        else:
            palette = array(typecode, palette)

        #if the color map doesn't start at zero
        #then we need to shift around the palette
        if head.color_map_origin:
            palette = (palette[head.color_map_origin: ] +
                       palette[: head.color_map_origin])

        tex_info.update(palette=[palette], palettize=1, indexing_size=bpp)
        pixel_array = array("B", pixels)
    elif bpp == 24:
        pixel_array = pad_24bit_array(pixels)
    elif bpp == 48:
        pixel_array = pad_48bit_array(pixels)
    else:
        pixel_array = array(typecode, pixels)

    tex_block.append(pixel_array)
    convertor.load_new_texture(texture_block=tex_block,
                               texture_info=tex_info)


def save_to_rawdata_file(convertor, output_path, ext, **kwargs):
    """Saves the currently loaded texture to a raw file.
    The file has no header and in most cases wont be able
    to be directly opened be applications."""

    final_output_path = output_path
    if not os.path.exists(os.path.dirname(final_output_path)):
        os.makedirs(os.path.dirname(output_path))

    filenames = []
    if convertor.is_palettized():
        print("Cannot save palettized images to RAW files.")
        return filenames

    fmt = convertor.format
    tex_block = convertor.texture_block
    sub_bitmap_ct = convertor.sub_bitmap_count
    overwrite      = kwargs.get("overwrite", True)
    mip_levels     = kwargs.get("mip_levels", (0, ))
    bitmap_indexes = kwargs.get("bitmap_indexes", "all")

    if bitmap_indexes == "all":
        bitmap_indexes = range(sub_bitmap_ct)
    elif isinstance(bitmap_indexes, int):
        bitmap_indexes = (bitmap_indexes, )

    if mip_levels == "all":
        mip_levels = range(convertor.mipmap_count + 1)
    elif isinstance(mip_levels, int):
        mip_levels = (mip_levels, )

    for m in mip_levels:
        width  = max(convertor.width  // (1<<m), 1)
        height = max(convertor.height // (1<<m), 1)
        depth  = max(convertor.depth  // (1<<m), 1)

        mip_output_path = output_path
        if len(mip_levels) > 1:
            mip_output_path = "%s_mip%s" % (mip_output_path, m)

        for sb in bitmap_indexes:
            index = sb + m*sub_bitmap_ct
            if index >= len(tex_block):
                continue

            final_output_path = mip_output_path
            if sub_bitmap_ct > 1:
                final_output_path = "%s_tex%s" % (final_output_path, sb)

            final_output_path = "%s.%s" % (final_output_path, ext)

            if not overwrite and os.path.exists(final_output_path):
                continue

            with open(final_output_path, 'wb+') as raw_file:
                pixel_array = tex_block[index]
                if not convertor.packed:
                    pixel_array = convertor.pack(
                        pixel_array, width, height, depth)
                    if pixel_array is None:
                        print("ERROR: UNABLE TO PACK IMAGE DATA.\n")
                        continue

                if ab.BITS_PER_PIXEL[fmt] == 24:
                    raw_file.write(unpad_24bit_array(pixel_array))
                elif ab.BITS_PER_PIXEL[fmt] == 48:
                    raw_file.write(unpad_48bit_array(pixel_array))
                else:
                    raw_file.write(pixel_array)
            filenames.append(final_output_path)

    return filenames


def save_to_dds_file(convertor, output_path, ext, **kwargs):
    """Saves the currently loaded texture to a DDS file"""
    typ = convertor.texture_type
    fmt = convertor.format

    swizzle_mode = kwargs.pop("swizzle_mode", convertor.swizzle_mode)
    channel_map = kwargs.pop("channel_mapping", None)
    if ((isinstance(channel_map, (list, tuple)) and 
         list(channel_map) != list(range(len(channel_map)))
         ) or
        convertor.swizzled != swizzle_mode or
        convertor.tiled or convertor.big_endian
        ):
        conv_cpy = deepcopy(convertor)
        conv_cpy.load_new_conversion_settings(
            swizzle_mode=swizzle_mode, tile_mode=False,
            channel_mapping=channel_map, target_big_endian=False)
        conv_cpy.byteswap_packed_endianness(False)
        conv_cpy.convert_texture()
        return conv_cpy.save_to_file(output_path="%s.%s" % (output_path, ext),
                                     **kwargs)

    if fmt in (ab.FORMAT_A16R16G16B16, ab.FORMAT_R16G16B16):
        print("ERROR: CANNOT SAVE %s TO DDS.\nCANCELLING DDS SAVE." %
              fmt)
        return []

    dds_file = dds_def.build()
    dds_file.data.pixel_data = b''
    dds_file.filepath = "%s.%s" % (output_path, ext)
    if not kwargs.get("overwrite", True) and os.path.exists(dds_file.filepath):
        return []

    mip_levels     = kwargs.get("mip_levels", "all")
    bitmap_indexes = kwargs.get("bitmap_indexes", "all")

    if bitmap_indexes == "all":
        bitmap_indexes = range(convertor.sub_bitmap_count)
    elif isinstance(bitmap_indexes, int):
        bitmap_indexes = (bitmap_indexes, )

    if mip_levels == "all":
        mip_levels = range(convertor.mipmap_count + 1)
    elif isinstance(mip_levels, int):
        mip_levels = (mip_levels, )

    head = dds_file.data.header
    flags = head.flags
    fmt_head  = head.dds_pixelformat
    fmt_flags = fmt_head.flags

    w, h, d = convertor.width, convertor.height, convertor.depth
    bpp = ab.BITS_PER_PIXEL[fmt]
    masks = ab.CHANNEL_MASKS[fmt]
    offsets = ab.CHANNEL_OFFSETS[fmt]
    channel_count = ab.CHANNEL_COUNTS[fmt]
    if fmt in ab.THREE_CHANNEL_FORMATS:
        channel_count = 3

    palette_unpacker  = convertor.palette_unpacker
    indexing_unpacker = convertor.indexing_unpacker
    depalettizer      = convertor.depalettize_bitmap

    flags.linearsize = True
    fmt_flags.four_cc = True

    # compute this for the block compressed formats
    head.pitch_or_linearsize = max(1, (w + 3) // 4) * ((bpp * 16) // 8)

    if fmt in (ab.FORMAT_DXT3A, ab.FORMAT_DXT3Y, ab.FORMAT_DXT3AY,
               ab.FORMAT_DXT5A, ab.FORMAT_DXT5Y, ab.FORMAT_DXT5AY,
               ab.FORMAT_CTX1):
        fmt_name = "LIN_%s" % fmt
        if fmt in (ab.FORMAT_DXT3AY, ab.FORMAT_DXT3Y, ab.FORMAT_DXT3A,
                   ab.FORMAT_DXT5AY, ab.FORMAT_DXT5Y, ab.FORMAT_DXT5A):
            fmt_name = fmt_name[:len("LIN_DXT_")] + "A"
            fmt_flags.luminance  = "Y" in fmt
            fmt_flags.alpha_only = not fmt_flags.luminance
            fmt_flags.has_alpha  = "A" in fmt and not fmt_flags.alpha_only

        fmt_head.four_cc.set_to(fmt_name)
        head.pitch_or_linearsize *= max(1, (h + 3) // 4)
    elif "DXT" in fmt.upper() or fmt in (ab.FORMAT_DXN, ):
        fmt_head.four_cc.set_to(fmt)
        head.pitch_or_linearsize *= max(1, (h + 3) // 4)
    elif fmt in (ab.FORMAT_A1, ab.FORMAT_A4, ab.FORMAT_L4, ab.FORMAT_A4L4,
                 ab.FORMAT_L5V5U5, ab.FORMAT_X8L8V8U8, ab.FORMAT_Q8L8V8U8,
                 ab.FORMAT_Q8W8V8U8, ab.FORMAT_Q16W16V16U16,
                 ab.FORMAT_R8G8, ab.FORMAT_R16G16, ab.FORMAT_A2W10V10U10,
                 ab.FORMAT_A2B10G10R10, ab.FORMAT_A2R10G10B10,
                 ab.FORMAT_A16B16G16R16):
        flags.linearsize = False
        flags.pitch = True
        fmt_head.four_cc.set_to(fmt)
    else:
        # non-fourcc format
        flags.linearsize = False
        flags.pitch = True
        head.pitch_or_linearsize = (w * bpp + 7) // 8

        fmt_flags.four_cc = False
        fmt_head.rgb_bitcount = bpp
        if fmt in (ab.FORMAT_V8U8, ab.FORMAT_V16U16):
            fmt_flags.vu_space = True
            fmt_head.r_bitmask = masks[1] << offsets[1]
            fmt_head.g_bitmask = masks[2] << offsets[2]
        elif channel_count >= 3:
            if fmt == ab.FORMAT_Y8U8V8:
                fmt_flags.yuv_space = True
            else:
                fmt_flags.rgb_space = True
                fmt_flags.has_alpha = channel_count > 3
                if fmt_flags.has_alpha:
                    fmt_head.a_bitmask = masks[0] << offsets[0]

            fmt_head.r_bitmask = masks[1] << offsets[1]
            fmt_head.g_bitmask = masks[2] << offsets[2]
            fmt_head.b_bitmask = masks[3] << offsets[3]
        elif channel_count == 2:
            fmt_head.a_bitmask = masks[0] << offsets[0]
            fmt_head.r_bitmask = masks[1] << offsets[1]
            fmt_flags.has_alpha = True
            fmt_flags.luminance = True
        elif fmt == ab.FORMAT_A8:
            fmt_head.a_bitmask = masks[0] << offsets[0]
            fmt_flags.alpha_only = True
        else:
            fmt_head.r_bitmask = masks[0] << offsets[0]
            fmt_flags.luminance = True

    head.width, head.height, head.depth = w, h, d
    if typ == ab.TYPE_3D:
        head.caps.complex = True
        head.caps2.volume = flags.depth = True
    elif typ == ab.TYPE_CUBEMAP:
        head.caps.complex = True
        head.caps2.cubemap = True
        for name in ("pos_x", "pos_y", "pos_z",
                     "neg_x", "neg_y", "neg_z")[: len(bitmap_indexes)]:
            head.caps2[name] = True

    head.mipmap_count = len(mip_levels) - 1
    if convertor.mipmap_count:
        head.caps.complex = True
        head.caps.mipmaps = flags.mipmaps = True
    if convertor.photoshop_compatability:
        head.mipmap_count += 1

    #write each of the pixel arrays into the bitmap
    for sb in bitmap_indexes:
        #write each of the pixel arrays into the bitmap
        for m in mip_levels:
            # get the index of the bitmap we'll be working with
            i = m*convertor.sub_bitmap_count + sb
            pixels = convertor.texture_block[i]
            w, h, d = ab.get_mipmap_dimensions(
                head.width, head.height, head.depth, m)

            if convertor.is_palettized():
                pal = convertor.palette[i]
                if convertor.palette_packed:
                    pal = palette_unpacker(pal)
                if convertor.packed:
                    pixels = indexing_unpacker(pixels)

                pixels = convertor.pack_raw(depalettizer(pal, pixels))
            elif not convertor.packed:
                pixels = convertor.pack(pixels, w, h, d)

            if pixels is None:
                print("ERROR: UNABLE TO PACK IMAGE DATA.\nCANCELLING WRITE.")
                return []

            if bpp == 24:
                pixels = unpad_24bit_array(pixels)
            dds_file.data.pixel_data += pixels

        if typ != ab.TYPE_CUBEMAP:
            dds_file.filepath = "%s_tex%s.%s" % (output_path, sb, ext)
            dds_file.serialize(temp=False, backup=False, calc_pointers=False)
            dds_file.data.pixel_data = b''

    if typ == ab.TYPE_CUBEMAP:
        dds_file.serialize(temp=False, backup=False, calc_pointers=False)

    return [dds_file.filepath]


def save_to_tga_file(convertor, output_path, ext, **kwargs):
    """Saves the currently loaded texture to a TGA file"""
    fmt = convertor.format
    filenames = []

    make_copy = fmt not in (
        ab.FORMAT_A1, ab.FORMAT_L8, ab.FORMAT_A8,
        ab.FORMAT_A1R5G5B5, ab.FORMAT_R8G8B8,
        ab.FORMAT_X8R8G8B8, ab.FORMAT_A8R8G8B8)

    swizzle_mode = kwargs.pop("swizzle_mode", convertor.swizzle_mode)
    if ("channel_mapping" in kwargs or (fmt != ab.FORMAT_A8R8G8B8 and make_copy)
        or convertor.swizzled != swizzle_mode
        or convertor.tiled or convertor.big_endian):
        conv_cpy = deepcopy(convertor)
        # TODO: optimize this so the only textures loaded in and converted
        # are the mip_levels and sub_bitmaps that were requested to be saved
        conv_cpy.load_new_conversion_settings(
            target_format=ab.FORMAT_A8R8G8B8, target_big_endian=False,
            swizzle_mode=swizzle_mode, tile_mode=False,
            channel_mapping=kwargs.pop("channel_mapping", None))
        conv_cpy.byteswap_packed_endianness(False)
        conv_cpy.convert_texture()
        return conv_cpy.save_to_file(output_path="%s.%s" % (output_path, ext),
                                     **kwargs)

    channel_count = ab.CHANNEL_COUNTS[fmt]

    tga_file = tga_def.build()
    head = tga_file.data.header
    image_desc = head.image_descriptor
    image_desc.screen_origin.set_to("upper_left")
    if convertor.is_palettized():
        head.has_color_map.set_to("yes")
        head.image_type.format.set_to("color_mapped_rgb")
        head.color_map_length = 2**convertor.indexing_size
        head.color_map_depth = ab.BITS_PER_PIXEL[fmt]

        head.bpp = 8
        if convertor.target_indexing_size > 8:
            head.bpp = convertor.indexing_size
    else:
        head.bpp = ab.BITS_PER_PIXEL[fmt]
        head.image_type.format.set_to("bw_8_bit")
        if channel_count > 1:
            image_desc.alpha_bit_count = ab.CHANNEL_DEPTHS[fmt][0]
        if channel_count > 2:
            head.image_type.format.set_to("unmapped_rgb")

    final_output_path = output_path
    pals = convertor.palette
    tex_block = convertor.texture_block
    sub_bitmap_ct = convertor.sub_bitmap_count
    overwrite      = kwargs.get("overwrite", True)
    mip_levels     = kwargs.get("mip_levels", (0, ))
    bitmap_indexes = kwargs.get("bitmap_indexes", "all")

    if bitmap_indexes == "all":
        bitmap_indexes = range(sub_bitmap_ct)
    elif isinstance(bitmap_indexes, int):
        bitmap_indexes = (bitmap_indexes, )

    if mip_levels == "all":
        mip_levels = range(convertor.mipmap_count + 1)
    elif isinstance(mip_levels, int):
        mip_levels = (mip_levels, )

    for m in mip_levels:
        width  = max(convertor.width  // (1<<m), 1)
        height = max(convertor.height // (1<<m), 1)
        depth  = max(convertor.depth  // (1<<m), 1)
        head.width  = width
        head.height = height*depth
        mip_output_path = output_path
        if len(mip_levels) > 1:
            mip_output_path = "%s_mip%s" % (mip_output_path, m)

        for sb in bitmap_indexes:
            index = sb + m*sub_bitmap_ct
            if index >= len(tex_block):
                continue

            final_output_path = mip_output_path
            if sub_bitmap_ct > 1:
                final_output_path = "%s_tex%s" % (final_output_path, sb)

            tga_file.filepath = "%s.%s" % (final_output_path, ext)
            if not overwrite and os.path.exists(tga_file.filepath):
                continue

            if convertor.is_palettized():
                pal = pals[index]
                idx = tex_block[index]
                if not convertor.palette_packed:
                    pal = convertor.palette_packer(pal)

                '''need to pack the indexing and make sure it's 8-bit
                   since TGA doesn't support less than 8 bit indexing'''

                if not convertor.packed:
                    idx = convertor.indexing_packer(idx)
                elif convertor.indexing_size < 8:
                    temp = convertor.target_indexing_size
                    convertor.target_indexing_size = 8
                    try:
                        idx = convertor.indexing_packer(
                            convertor.indexing_unpacker(idx))
                    except Exception as e:
                        convertor.target_indexing_size = temp
                        raise e
                    finally:
                        convertor.target_indexing_size = temp

                tga_file.data.color_table = bytes(pal)
                tga_file.data.pixels_wrapper.pixels = idx
            else:
                pixel_array = tex_block[index]
                if not convertor.packed:
                    pixel_array = convertor.pack(pixel_array, width, height, 0)
                    if pixel_array is None:
                        print("ERROR: UNABLE TO PACK IMAGE DATA.\n"+
                              "CANCELLING TGA SAVE.")
                        return filenames

                if ab.BITS_PER_PIXEL[fmt] == 24:
                    pixel_array = unpad_24bit_array(pixel_array)

                tga_file.data.pixels_wrapper.pixels = pixel_array

            tga_file.serialize(temp=False, backup=False, calc_pointers=False)
            filenames.append(tga_file.filepath)

    return filenames


def save_to_png_file(convertor, output_path, ext, **kwargs):
    """Saves the currently loaded texture to a PNG file"""
    fmt = convertor.format
    palettized = convertor.is_palettized()
    swizzle_mode = kwargs.pop("swizzle_mode", convertor.swizzle_mode)
    channel_map = kwargs.pop("channel_mapping", None)
    merge_map = kwargs.pop("channel_merge_mapping", None)

    if channel_map:
        channel_count = len(channel_map)
    elif fmt in ab.THREE_CHANNEL_FORMATS:
        channel_count = 3
    else:
        channel_count = ab.CHANNEL_COUNTS[fmt]

    if channel_count <= 2:
        valid_depths = (1, 2, 4, 8, 16)
    else:
        valid_depths = (8, 16)

    bit_depth = fmt_depth = max(1, *ab.CHANNEL_DEPTHS[fmt])
    target_depth = 1 << int(ceil(log(bit_depth, 2)))
    keep_alpha = kwargs.get("keep_alpha", channel_count <= 2)
    save_as_rgb = kwargs.get("save_as_rgb", channel_count >= 2)

    filenames = []

    if save_as_rgb:
        # png doesnt allow 2 channel greyscale, so convert them to 4 channel
        if keep_alpha and channel_count != 3:
            if target_depth > 8: fmt_to_save_as = ab.FORMAT_A16R16G16B16
            else:                fmt_to_save_as = ab.FORMAT_A8R8G8B8
        else:
            if target_depth > 8: fmt_to_save_as = ab.FORMAT_R16G16B16
            else:                fmt_to_save_as = ab.FORMAT_R8G8B8

        if channel_count == 2:
            a_chan = 0 if (keep_alpha and channel_count != 3) else -1
            channel_map = (a_chan, 1) if channel_map is None else channel_map
            channel_map += (channel_map[1], ) * (4 - len(channel_map))
    elif fmt == ab.FORMAT_A16: fmt_to_save_as = fmt = ab.FORMAT_L16
    elif fmt == ab.FORMAT_A8:  fmt_to_save_as = fmt = ab.FORMAT_L8
    # FORMAT_A4/2/1 are not implemented yet
    #elif fmt == ab.FORMAT_A4:  fmt_to_save_as = fmt = ab.FORMAT_L4
    #elif fmt == ab.FORMAT_A2:  fmt_to_save_as = fmt = ab.FORMAT_L2
    #elif fmt == ab.FORMAT_A1:  fmt_to_save_as = fmt = ab.FORMAT_L1
    elif bit_depth > 8:      fmt_to_save_as = ab.FORMAT_L16
    elif target_depth == 8:  fmt_to_save_as = ab.FORMAT_L8
    # FORMAT_L4/2/1 are not implemented yet
    elif target_depth in (4, 2, 1):  fmt_to_save_as = ab.FORMAT_L8
    #elif target_depth == 4:  fmt_to_save_as = ab.FORMAT_L4
    #elif target_depth == 2:  fmt_to_save_as = ab.FORMAT_L2
    #elif target_depth == 1:  fmt_to_save_as = ab.FORMAT_L1

    if (fmt != fmt_to_save_as or bit_depth not in valid_depths or
        channel_map is not None or merge_map is not None or
        convertor.swizzled != swizzle_mode or convertor.tiled):

        conv_cpy = deepcopy(convertor)
        # TODO: optimize this so the only textures loaded in and converted
        # are the mip_levels and sub_bitmaps that were requested to be saved
        if target_depth > 8:
            conv_cpy.set_deep_color_mode(True)

        conv_cpy.load_new_conversion_settings(
            target_format=fmt_to_save_as, target_big_endian=False,
            swizzle_mode=swizzle_mode, tile_mode=False,
            channel_mapping=channel_map, channel_merge_mapping=merge_map)
        #conv_cpy.print_info(1,1,1)
        if not conv_cpy.convert_texture():
            return []
        return conv_cpy.save_to_file(output_path="%s.%s" % (output_path, ext),
                                     **kwargs)

    png_file = png_def.build()
    png_file.data.chunks.append(case="IHDR")
    head = png_file.data.chunks[-1]
    plte_chunk = None
    if palettized:
        bit_depth = convertor.indexing_size
        color_type = "indexed_color"
        png_file.data.chunks.append(case="sRGB")
        png_file.data.chunks.append(case="PLTE")
        plte_chunk = png_file.data.chunks[-1]
        if channel_count == 4:
            png_file.data.chunks.append(case="tRNS")
            trns_chunk = png_file.data.chunks[-1]
    elif channel_count > 2:
        color_type = "truecolor"
        if channel_count == 4:
            color_type = "truecolor_with_alpha"

        png_file.data.chunks.append(case="sRGB")
    elif channel_count == 2:
        color_type = "greyscale_with_alpha"
    else:
        color_type = "greyscale"

    head.bit_depth = bit_depth
    head.color_type.set_to(color_type)

    png_file.data.chunks.append(case="IDAT")
    idat_chunk = png_file.data.chunks[-1]
    png_file.data.chunks.append(case="IEND")

    tex_block = convertor.texture_block
    pals = convertor.palette
    sub_bitmap_ct = convertor.sub_bitmap_count
    overwrite      = kwargs.get("overwrite", True)
    mip_levels     = kwargs.get("mip_levels", (0, ))
    bitmap_indexes = kwargs.get("bitmap_indexes", "all")
    png_compress_level = kwargs.get("png_compress_level", None)

    if bitmap_indexes == "all":
        bitmap_indexes = range(sub_bitmap_ct)
    elif isinstance(bitmap_indexes, int):
        bitmap_indexes = (bitmap_indexes, )

    if mip_levels == "all":
        mip_levels = range(convertor.mipmap_count + 1)
    elif isinstance(mip_levels, int):
        mip_levels = (mip_levels, )

    for m in mip_levels:
        width  = max(convertor.width  >> m, 1)
        height = max(convertor.height >> m, 1)
        depth  = max(convertor.depth  >> m, 1)
        head.width  = width
        head.height = height*depth
        mip_output_path = output_path
        if len(mip_levels) > 1:
            mip_output_path = "%s_mip%s" % (mip_output_path, m)

        bitmap_size = head.width * head.height
        if not palettized:
            bitmap_size = (bitmap_size * channel_count * head.bit_depth) // 8

        for sb in bitmap_indexes:
            index = sb + m*sub_bitmap_ct
            if index >= len(tex_block):
                continue

            final_output_path = mip_output_path
            if sub_bitmap_ct > 1:
                final_output_path = "%s_tex%s" % (final_output_path, sb)

            png_file.filepath = "%s.%s" % (final_output_path, ext)
            if not overwrite and os.path.exists(png_file.filepath):
                continue

            stride = width * head.bit_depth
            pix = tex_block[index]
            if palettized:
                pal = pals[index]
                if not convertor.packed:
                    pix = convertor.indexing_packer(pix)

                if channel_count == 4:
                    if convertor.palette_packed:
                        pal = convertor.palette_unpacker(pal)
                    alpha_pal = array(
                        "B", (pal[i] for i in range(0, len(pal), 4)))
                    trns_chunk.palette = alpha_pal
                    old_pal = pal
                    pal = bytearray(len(pal)*3//4)
                    for i in range(len(pal)//3):
                        j = i*4
                        i = i*3
                        pal[i: i+3] = old_pal[j+1: j+4]
                    plte_chunk.data = pal
                else:
                    if not convertor.palette_packed:
                        pal = convertor.palette_packer(pal)
                    plte_chunk.data = bytes(pal)

            else:
                stride *= channel_count
                if channel_count == 1 and fmt_depth == 16:
                    stride = stride//2

                if not convertor.packed:
                    pix = convertor.pack_raw(pix)

                if channel_count <= 2:
                    pix = bytearray(pix)
                    if fmt_depth == 16:
                        stride *= 2  # no idea why this is needed, but it is...
                        if channel_count != 1:
                            swap_array_items(pix, (1, 0))
                elif channel_count == 4:
                    pix = bytearray(pix)
                    if   fmt_depth == 8:
                        swap_array_items(pix, (2, 1, 0, 3))
                    elif fmt_depth == 16:
                        swap_array_items(pix, (5, 4, 3, 2, 1, 0, 7, 6))
                elif fmt_depth == 8:
                    pix = bytearray(unpad_24bit_array(pix))
                    swap_array_items(pix, (2, 1, 0))
                elif fmt_depth == 16:
                    pix = bytearray(unpad_48bit_array(pix))
                    swap_array_items(pix, (5, 4, 3, 2, 1, 0))
                else:
                    pix = bytearray(pix)

            # png's NEED the pixel data to be the exact right size
            # too large and it'll crash upon tkinter loading
            if len(pix) < bitmap_size:
                pix += b'\x00' * (bitmap_size - len(pix))
            elif len(pix) > bitmap_size:
                pix = pix[: bitmap_size]

            png_file.set_chunk_data(
                idat_chunk, pad_idat_data(pix, stride//8),
                png_compress_level=png_compress_level)
            png_file.serialize(temp=False, backup=False, calc_pointers=False)
            filenames.append(png_file.filepath)

    return filenames


def get_pixel_bytes_size(fmt, width, height, depth=1, mip=0, tiled=False):
    width, height, depth = (ab.packed_dimension_calc(dim, mip, tiled)
                            for dim in (width, height, depth))

    if ab.PACKED_SIZE_CALCS.get(fmt):
        return ab.PACKED_SIZE_CALCS[fmt](fmt, width, height, depth)
    return (ab.BITS_PER_PIXEL[fmt] * height * width * depth)//8


def make_array(typecode, item_ct, item_size=None, fill=0):
    # it would be nice to be able to make an array of w/e size
    # without having to create a bytearray first and throw it away
    if item_size is None:
        item_size = PIXEL_ENCODING_SIZES.get(typecode, 1)
    return array(typecode, bytes([fill])*item_size)*item_ct


def crop_pixel_data(pix, chan_ct, width, height, depth,
                    x0=0, x1=-1, y0=0, y1=-1, z0=0, z1=-1):
    if x1 < 0: x1 = width
    if y1 < 0: y1 = height
    if z1 < 0: z1 = depth

    new_pix = make_array(
        pix.typecode,
        (x1 - x0) * (y1 - y0) * (z1 - z0) * chan_ct,
        pix.itemsize)

    if len(pix) == 0:
        return new_pix

    pixel_width = chan_ct * pix.itemsize

    src_x_skip0, src_y_skip0, src_z_skip0 = max(x0, 0), max(y0, 0), max(z0, 0)

    src_x_skip1 = max(width  - src_x_skip0 - x1, 0)
    src_y_skip1 = max(height - src_y_skip0 - y1, 0)
    src_z_skip1 = max(depth  - src_z_skip0 - z1, 0)

    x_stride = width  - src_x_skip0 - src_x_skip1
    y_stride = height - src_y_skip0 - src_y_skip1
    z_stride = depth  - src_z_skip0 - src_z_skip1

    if 0 in (x_stride, y_stride, z_stride):
        return new_pix

    dst_x_skip0, dst_y_skip0, dst_z_skip0 = max(-x0, 0), max(-y0, 0), max(-z0, 0)

    dst_x_skip1 = max(x1 - dst_x_skip0 - x_stride, 0)
    dst_y_skip1 = max(y1 - dst_y_skip0 - y_stride, 0)

    src_z_skip0 *= width * pixel_width * height
    src_y_skip0 *= width * pixel_width
    src_y_skip1 *= width * pixel_width
    dst_z_skip0 *= x1 * pixel_width * y1
    dst_y_skip0 *= x1 * pixel_width
    dst_y_skip1 *= x1 * pixel_width

    src_x_skip0 *= pixel_width
    dst_x_skip0 *= pixel_width
    src_x_skip1 *= pixel_width
    dst_x_skip1 *= pixel_width
    x_stride *= pixel_width

    if fast_bitmap_io:
        bitmap_io_ext.crop_pixel_data(
            pix, new_pix, z_stride, y_stride, x_stride,
            src_z_skip0, dst_z_skip0,
            src_y_skip0, dst_y_skip0, src_x_skip0, dst_x_skip0,
            src_y_skip1, dst_y_skip1, src_x_skip1, dst_x_skip1)
        return new_pix

    with memoryview(pix).cast("B") as src, memoryview(new_pix).cast("B") as dst:
        src_i = src_z_skip0
        dst_i = dst_z_skip0
        src_x_skip1 += x_stride
        dst_x_skip1 += x_stride
        for z in range(z_stride):
            src_i += src_y_skip0
            dst_i += dst_y_skip0
            for y in range(y_stride):
                src_i += src_x_skip0
                dst_i += dst_x_skip0
                dst[dst_i: dst_i + x_stride] = src[src_i: src_i + x_stride]
                src_i += src_x_skip1
                dst_i += dst_x_skip1

            src_i += src_y_skip1
            dst_i += dst_y_skip1

    return new_pix


def swap_array_items(pix, channel_map, adapt_to_itemsize=False):
    step = len(channel_map)
    if adapt_to_itemsize and isinstance(pix, array):
        step *= pix.itemsize
        itemsize = pix.itemsize
        channel_map = [channel_map[i // itemsize] * itemsize + (i % itemsize)
                       for i in range(len(itemsize * channel_map))]

    channel_map = tuple(channel_map)
    src_map     = tuple(range(step))
    if channel_map == src_map:
        # no mapping difference. return without doing anything
        return

    for c in channel_map:
        assert c < len(channel_map)

    if fast_bitmap_io:
        bitmap_io_ext.swap_array_items(pix, array("h", channel_map))
        return

    with memoryview(pix).cast("B") as pix_bytes:
        for i in range(0, len(pix_bytes), step):
            orig = bytes(pix_bytes[i: i + step])
            for j in src_map:
                if channel_map[j] < 0:
                    pix_bytes[i + j] = 0
                else:
                    pix_bytes[i + j] = orig[channel_map[j]]


def bitmap_bytes_to_array(rawdata, offset, texture_block, fmt,
                          width, height, depth=1, bitmap_size=None, **kwargs):
    """This function will create an array of pixels of width*height*depth from
    an iterable, sliceable, object, and append it to the supplied texture_block.
    This function will return the offset of the end of the pixel data so that
    textures following the current one can be found."""
    #get the texture encoding
    encoding = ab.PACKED_TYPECODES[fmt]

    pixel_size = ab.PIXEL_ENCODING_SIZES[encoding]

    #get how many bytes the texture is going to be if it wasnt provided
    if bitmap_size is None:
        bitmap_size = bitmap_data_end = get_pixel_bytes_size(
            fmt, width, height, depth)
    bitmap_data_end = bitmap_size

    '''24 bit images are handled a bit differently since lots of
    things work on whole powers of 2. "2" can not be raised to an
    integer power to yield "24", whereas it can be for 8, 16, and 32.
    To fix this, the bitmap will be padded with an alpha channel on
    loading and ignored on saving. This will bring the 24 bit image
    up to 32 bit and make everything work just fine.'''
    if ab.BITS_PER_PIXEL[fmt] == 24:
        pixel_array = pad_24bit_array(rawdata[offset: offset + bitmap_size])
    elif ab.BITS_PER_PIXEL[fmt] == 48:
        pixel_array = pad_48bit_array(rawdata[offset: offset + bitmap_size])
    else:
        pixel_array = array(encoding, rawdata[offset: offset + bitmap_size])

    #if not enough pixel data was supplied, extra will be added
    if len(pixel_array)*pixel_size < bitmap_size:
        #print("WARNING: PIXEL DATA SUPPLIED DID NOT MEET "+
        #      "THE SIZE EXPECTED. PADDING WITH ZEROS.")
        itemsize = PIXEL_ENCODING_SIZES.get(pixel_array.typecode, 1)
        pixel_array.extend(
            make_array(pixel_array.typecode,
                       (bitmap_size - len(pixel_array)*pixel_size) // itemsize,
                       itemsize))

    #add the pixel array to the current texture block
    texture_block.append(pixel_array)
    return offset + bitmap_data_end


def bitmap_palette_to_array(rawdata, offset, palette_block, fmt, palette_count):
    return bitmap_bytes_to_array(rawdata, offset, palette_block,
                                 fmt, palette_count, 1)


def bitmap_indexing_to_array(rawdata, offset, indexing_block,
                             width, height, depth=1):
    indexing_block.append(array("B", rawdata[offset:offset+width*height*depth]))
    return offset + width*height*depth


def pad_24bit_array(unpadded):
    if not hasattr(unpadded, 'typecode'):
        unpadded = array("B", unpadded)
    elif unpadded.typecode != 'B':
        raise TypeError(
            "Bad typecode for unpadded 24bit array. Expected B, got %s" %
            unpadded.typecode)

    if fast_bitmap_io:
        padded = make_array("I", len(unpadded)//3)
        bitmap_io_ext.pad_24bit_array(padded, unpadded)
        return padded

    return array(
        "I", map(lambda x:(
            unpadded[x] + (unpadded[x+1]<<8)+ (unpadded[x+2]<<16)),
                 range(0, len(unpadded), 3)))


def pad_48bit_array(unpadded):
    if not hasattr(unpadded, 'typecode'):
        unpadded = array("B", unpadded)
    elif unpadded.typecode != 'B':
        raise TypeError(
            "Bad typecode for unpadded 24bit array. Expected B, got %s" %
            unpadded.typecode)

    if fast_bitmap_io:
        padded = make_array("Q", len(unpadded)//3)
        bitmap_io_ext.pad_48bit_array(padded, unpadded)
        return padded

    return array(
        "Q", map(lambda x:(
            unpadded[x] + (unpadded[x+1]<<16)+ (unpadded[x+2]<<32)),
                 range(0, len(unpadded), 3)))


def unpad_24bit_array(padded):
    """given a 24BPP pixel data array that has been padded to
    32BPP, this will return an unpadded, unpacked, array copy.
    The endianness of the data will be little."""

    if padded.typecode == "I":
        # pixels have been packed
        unpadded = make_array("B", len(padded), 3)
        if fast_bitmap_io:
            bitmap_io_ext.unpad_24bit_array(unpadded, padded)
        else:
            for i in range(len(padded)):
                unpadded[i*3]   = padded[i]&255
                unpadded[i*3+1] = (padded[i]>>8)&255
                unpadded[i*3+2] = (padded[i]>>16)&255
    elif padded.typecode == "B":
        # pixels have NOT been packed

        # Because they havent been packed, it should be assumed
        # the channel order is the default one, namely ARGB.
        # Since we are removing the alpha channel, remove
        # the first byte from each pixel
        unpadded = make_array("B", len(padded)//4, 3)
        if fast_bitmap_io:
            bitmap_io_ext.unpad_24bit_array(unpadded, padded)
        else:
            for i in range(len(padded)//4):
                unpadded[i*3]   = padded[i*4+1]
                unpadded[i*3+1] = padded[i*4+2]
                unpadded[i*3+2] = padded[i*4+3]
    else:
        raise TypeError(
            "Bad typecode for padded 24bit array. Expected B or I, got %s" %
            padded.typecode)

    return unpadded


def unpad_48bit_array(padded):
    """given a 48BPP pixel data array that has been padded to
    64BPP, this will return an unpadded, unpacked, array copy.
    The endianness of the data will be little."""

    if padded.typecode == "Q":
        # pixels have been packed
        unpadded = make_array("H", len(padded), 6)
        if fast_bitmap_io:
            bitmap_io_ext.unpad_48bit_array(unpadded, padded)
        else:
            for i in range(len(padded)):
                j = i*3
                unpadded[j]   = padded[i]&65535
                unpadded[j+1] = (padded[i]>>16)&65535
                unpadded[j+2] = (padded[i]>>32)&65535
    elif padded.typecode == "H":
        # pixels have NOT been packed

        # Because they havent been packed, it should be assumed
        # the channel order is the default one, namely ARGB.
        # Since we are removing the alpha channel, remove
        # the first two bytes from each pixel
        unpadded = make_array("H", len(padded)//4, 6)
        if fast_bitmap_io:
            bitmap_io_ext.unpad_48bit_array(unpadded, padded)
        else:
            for i in range(len(padded)//4):
                j = i*3
                i *= 4
                unpadded[j]   = padded[i+1]
                unpadded[j+1] = padded[i+2]
                unpadded[j+2] = padded[i+3]
    else:
        raise TypeError(
            "Bad typecode for padded 48bit array. Expected H or Q, got %s" %
            padded.typecode)

    return unpadded


def byteswap_packed_bitmap(packed_pixel_data, fmt):
    channel_map = []
    i = 0
    for size in ab.PACKED_FIELD_SIZES[fmt]:
        channel_map.extend(list(range(i, i + size))[::-1])
        i += size

    swap_array_items(packed_pixel_data, channel_map)


file_writers = {"raw":save_to_rawdata_file, "bin":save_to_rawdata_file}
file_readers = {}

if tga_def is not None:
    file_writers["tga"] = save_to_tga_file
    file_readers["tga"] = load_from_tga_file

if dds_def is not None:
    file_writers["dds"] = save_to_dds_file
    file_readers["dds"] = load_from_dds_file

if png_def is not None:
    file_writers["png"] = save_to_png_file
