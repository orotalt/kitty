#!/usr/bin/env python
# vim:fileencoding=utf-8
# License: GPL v3 Copyright: 2016, Kovid Goyal <kovid at kovidgoyal.net>

import os
import sys
from collections import namedtuple
from contextlib import contextmanager
from ctypes import addressof, c_char, sizeof
from functools import lru_cache

from .fast_data_types import (
    GL_ARRAY_BUFFER, GL_CLAMP_TO_EDGE, GL_FLOAT, GL_MAX_ARRAY_TEXTURE_LAYERS,
    GL_MAX_TEXTURE_SIZE, GL_NEAREST, GL_R8, GL_RED, GL_STREAM_DRAW,
    GL_TEXTURE0, GL_TEXTURE_2D_ARRAY, GL_TEXTURE_MAG_FILTER,
    GL_TEXTURE_MIN_FILTER, GL_TEXTURE_WRAP_S, GL_TEXTURE_WRAP_T,
    GL_UNIFORM_BUFFER, GL_UNPACK_ALIGNMENT, GL_UNSIGNED_BYTE, GL_WRITE_ONLY,
    GLSL_VERSION, compile_program, copy_image_sub_data,
    get_uniform_block_offsets, get_uniform_block_size, glActiveTexture,
    glBindBuffer, glBindBufferBase, glBindTexture, glBindVertexArray,
    glCopyImageSubData, glDeleteBuffer, glDeleteTexture, glDeleteVertexArray,
    glEnableVertexAttribArray, glGenBuffers, glGenTextures, glGenVertexArrays,
    glGetAttribLocation, glGetBufferSubData, glGetIntegerv,
    glGetUniformBlockIndex, glGetUniformLocation, glMapBuffer, glPixelStorei,
    glTexParameteri, glTexStorage3D, glTexSubImage3D, glUnmapBuffer,
    glUseProgram, glVertexAttribDivisor, glVertexAttribPointer,
    render_dirty_sprites, replace_or_create_buffer, sprite_map_current_layout,
    sprite_map_free, sprite_map_increment, sprite_map_set_layout,
    sprite_map_set_limits
)
from .fonts.render import render_cell
from .utils import safe_print

UBO = namedtuple('UBO', 'size index offsets buf_id')
BASE = os.path.dirname(os.path.abspath(__file__))


@lru_cache()
def load_shaders(name):
    vert = open(os.path.join(BASE, '{}_vertex.glsl'.format(name))).read().replace('GLSL_VERSION', str(GLSL_VERSION), 1)
    frag = open(os.path.join(BASE, '{}_fragment.glsl'.format(name))).read().replace('GLSL_VERSION', str(GLSL_VERSION), 1)
    return vert, frag


class BufferManager:  # {{{

    def __init__(self):
        self.sizes = {}
        self.types = {}
        self.ctypes_types = {}
        self.name_count = 0

    def create(self, for_use=GL_ARRAY_BUFFER):
        buf_id = glGenBuffers(1)
        self.types[buf_id] = for_use
        self.sizes.pop(buf_id, None)
        self.ctypes_types.pop(buf_id, None)
        return buf_id

    def delete(self, buf_id):
        if buf_id in self.types:
            glDeleteBuffer(buf_id)
            self.sizes.pop(buf_id, None)
            self.types.pop(buf_id)
            self.ctypes_types.pop(buf_id, None)

    def set_data(self, buf_id, data, usage=GL_STREAM_DRAW, verify=False):
        prev_sz = self.sizes.get(buf_id, 0)
        new_sz = sizeof(data)
        replace_or_create_buffer(buf_id, new_sz, prev_sz, addressof(data), usage, self.types[buf_id])
        self.sizes[buf_id] = new_sz
        self.ctypes_types[buf_id] = type(data)
        if verify:
            verify_data = self.get_data(buf_id)
            if list(data) != list(verify_data):
                raise RuntimeError('OpenGL failed to upload to buffer')

    def get_data(self, buf_id):
        verify_data = self.ctypes_types[buf_id]()
        glGetBufferSubData(self.types[buf_id], buf_id, self.sizes[buf_id], 0, addressof(verify_data))
        return verify_data

    def bind(self, buf_id):
        glBindBuffer(self.types[buf_id], buf_id)

    def unbind(self, buf_id):
        glBindBuffer(self.types[buf_id], 0)

    @contextmanager
    def bound_buffer(self, buf_id):
        self.bind(buf_id)
        yield
        self.unbind(buf_id)

    @contextmanager
    def mapped_buffer(self, buf_id, buf_sz, usage=GL_STREAM_DRAW, access=GL_WRITE_ONLY):
        prev_sz = self.sizes.get(buf_id, 0)
        buf_type = self.types[buf_id]
        if prev_sz != buf_sz:
            replace_or_create_buffer(buf_id, buf_sz, 0, 0, usage, buf_type)
            self.sizes[buf_id] = buf_sz
            self.ctypes_types[buf_id] = c_char
        with self.bound_buffer(buf_id):
            address = glMapBuffer(buf_type, access)
            yield address
            glUnmapBuffer(buf_type)


buffer_manager = BufferManager()
# }}}


class Sprites:  # {{{
    ''' Maintain sprite sheets of all rendered characters on the GPU as a texture
    array with each texture being a sprite sheet. '''

    # TODO: Rewrite this class using the ARB_shader_image_load_store and ARB_shader_storage_buffer_object
    # extensions one they become available.

    def __init__(self):
        self.xnum = self.ynum = 1
        self.first_cell_cache = {}
        self.second_cell_cache = {}
        self.x = self.y = self.z = 0
        self.texture_id = None
        self.last_num_of_layers = 1
        self.last_ynum = -1
        self.sampler_num = 0
        self.texture_unit = GL_TEXTURE0 + self.sampler_num
        sprite_map_set_limits(glGetIntegerv(GL_MAX_TEXTURE_SIZE), glGetIntegerv(GL_MAX_ARRAY_TEXTURE_LAYERS))

    def do_layout(self, cell_width=1, cell_height=1):
        self.cell_width, self.cell_height = cell_width, cell_height
        sprite_map_set_layout(cell_width or 1, cell_height or 1)
        if self.texture_id is not None:
            glDeleteTexture(self.texture_id)
            self.texture_id = None
        self.ensure_state()
        # Pre-render the basic cells to ensure they have known sprite numbers

        def send(*a, **kw):
            buf = render_cell(*a, **kw)[0]
            x, y, z = sprite_map_increment()
            self.send_to_gpu(x, y, z, buf)
            return x

        send()  # blank
        send(underline=1)
        send(underline=2)
        if send(strikethrough=True) != 3:
            raise RuntimeError('Available OpenGL texture size is too small')

    @property
    def layout(self):
        xnum, ynum, znum = sprite_map_current_layout()
        return 1 / xnum, 1 / ynum

    def render_cell(self, text, bold, italic, is_second):
        first, second = render_cell(text, bold, italic)
        ans = second if is_second else first
        return ans or render_cell()[0]

    def render_dirty_sprites(self):
        ret = render_dirty_sprites()
        if ret:
            for text, bold, italic, is_second, x, y, z in ret:
                cell = self.render_cell(text, bold, italic, is_second)
                self.send_to_gpu(x, y, z, cell)

    def send_to_gpu(self, x, y, z, buf):
        xnum, ynum, znum = sprite_map_current_layout()
        if znum >= self.last_num_of_layers:
            self.realloc_texture()
        else:
            if znum == 0 and ynum > self.last_ynum:
                self.realloc_texture()
        tgt = GL_TEXTURE_2D_ARRAY
        glBindTexture(tgt, self.texture_id)
        glPixelStorei(GL_UNPACK_ALIGNMENT, 1)
        x, y = x * self.cell_width, y * self.cell_height
        glTexSubImage3D(tgt, 0, x, y, z, self.cell_width, self.cell_height, 1, GL_RED, GL_UNSIGNED_BYTE, addressof(buf))

    def realloc_texture(self):
        tgt = GL_TEXTURE_2D_ARRAY
        tex = glGenTextures(1)
        glBindTexture(tgt, tex)
        # We use GL_NEAREST otherwise glyphs that touch the edge of the cell
        # often show a border between cells
        glTexParameteri(tgt, GL_TEXTURE_MIN_FILTER, GL_NEAREST)
        glTexParameteri(tgt, GL_TEXTURE_MAG_FILTER, GL_NEAREST)
        glTexParameteri(tgt, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
        glTexParameteri(tgt, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
        xnum, bynum, z = sprite_map_current_layout()
        znum = z + 1
        width, height = xnum * self.cell_width, bynum * self.cell_height
        glTexStorage3D(tgt, 1, GL_R8, width, height, znum)
        if self.texture_id is not None:
            ynum = bynum
            if z == 0:
                ynum -= 1  # Only copy the previous rows
            try:
                glCopyImageSubData(self.texture_id, tgt, 0, 0, 0, 0, tex, tgt, 0, 0, 0, 0,
                                   width, ynum * self.cell_height, self.last_num_of_layers)
            except RuntimeError:
                # OpenGL does not have ARB_copy_image
                if not hasattr(self, 'realloc_warned'):
                    safe_print(
                        'WARNING: Your system\'s OpenGL implementation does not have glCopyImageSubData, falling back to a slower implementation',
                        file=sys.stderr)
                    self.realloc_warned = True
                copy_image_sub_data(self.texture_id, tex, width, ynum * self.cell_height, self.last_num_of_layers)
                glBindTexture(tgt, tex)
            glDeleteTexture(self.texture_id)
        self.last_num_of_layers = znum
        self.last_ynum = bynum
        self.texture_id = tex

    def destroy(self):
        sprite_map_free()
        if self.texture_id is not None:
            glDeleteTexture(self.texture_id)
            self.texture_id = None

    def ensure_state(self):
        if self.texture_id is None:
            self.realloc_texture()

    def __enter__(self):
        self.ensure_state()
        glActiveTexture(self.texture_unit)
        glBindTexture(GL_TEXTURE_2D_ARRAY, self.texture_id)

    def __exit__(self, *a):
        glBindTexture(GL_TEXTURE_2D_ARRAY, 0)

# }}}


class ShaderProgram:  # {{{
    """ Helper class for using GLSL shader programs """

    def __init__(self, which, vertex, fragment):
        """
        Create a shader program.

        """
        self.program_id = compile_program(which, vertex, fragment)
        self.vertex_arrays = {}

    @contextmanager
    def array_object_creator(self):
        vao_id = glGenVertexArrays(1)
        self.vertex_arrays[vao_id] = buffers = []
        buf_id = None

        def newbuf():
            nonlocal buf_id
            buf_id = buffer_manager.create(for_use=GL_ARRAY_BUFFER)
            buffers.append(buf_id)
            return buf_id

        def add_attribute(name, size=3, dtype=GL_FLOAT, normalized=False, stride=0, offset=0, divisor=0):
            nonlocal buf_id
            aid = self.attribute_location(name)
            if aid > -1:
                if buf_id is None:
                    buf_id = newbuf()
                with buffer_manager.bound_buffer(buf_id):
                    glEnableVertexAttribArray(aid)
                    glVertexAttribPointer(aid, size, dtype, normalized, stride, offset)
                    if divisor > 0:
                        glVertexAttribDivisor(aid, divisor)

        add_attribute.newbuf = newbuf
        add_attribute.vao_id = vao_id
        with self.bound_vertex_array(vao_id):
            yield add_attribute

    @contextmanager
    def bound_vertex_array(self, vao_id):
        glBindVertexArray(vao_id)
        yield
        glBindVertexArray(0)

    def remove_vertex_array(self, vao_id):
        buffers = self.vertex_arrays.pop(vao_id, None)
        if buffers is not None:
            glDeleteVertexArray(vao_id)
            for buf_id in buffers:
                buffer_manager.delete(buf_id)

    def send_vertex_data(self, vao_id, data, usage=GL_STREAM_DRAW, bufnum=0):
        bufid = self.vertex_arrays[vao_id][bufnum]
        buffer_manager.set_data(bufid, data, usage=usage)

    def mapped_vertex_data(self, vao_id, buf_sz, usage=GL_STREAM_DRAW, bufnum=0, access=GL_WRITE_ONLY):
        bufid = self.vertex_arrays[vao_id][bufnum]
        return buffer_manager.mapped_buffer(bufid, buf_sz, usage=usage, access=access)

    def get_vertex_data(self, vao_id, bufnum=0):
        bufid = self.vertex_arrays[vao_id][bufnum]
        return buffer_manager.get_data(bufid)

    def __hash__(self) -> int:
        return self.program_id

    def __eq__(self, other) -> bool:
        return isinstance(other, ShaderProgram) and other.program_id == self.program_id

    def __ne__(self, other) -> bool:
        return not self.__eq__(other)

    @lru_cache(maxsize=2**6)
    def uniform_location(self, name: str) -> int:
        ' Return the id for the uniform variable `name` or -1 if not found. '
        return glGetUniformLocation(self.program_id, name)

    @lru_cache(maxsize=2**6)
    def uniform_block_index(self, name: str) -> int:
        ' Return the block index for the specified uniform block raises ValueError if not found. '
        return glGetUniformBlockIndex(self.program_id, name)

    def uniform_block_size(self, block_index):
        return get_uniform_block_size(self.program_id, block_index)

    def init_uniform_block(self, name, *variables):
        idx = self.uniform_block_index(name)
        size = self.uniform_block_size(idx)
        offsets = get_uniform_block_offsets(self.program_id, variables)
        offsets = dict(zip(variables, offsets))
        buf_id = buffer_manager.create(GL_UNIFORM_BUFFER)
        return UBO(size=size, index=idx, offsets=offsets, buf_id=buf_id)

    def mapped_uniform_data(self, ubo, usage=GL_STREAM_DRAW, access=GL_WRITE_ONLY):
        return buffer_manager.mapped_buffer(ubo.buf_id, ubo.size, usage=usage, access=access)

    @contextmanager
    def bound_uniform_buffer(self, ubo):
        glBindBufferBase(GL_UNIFORM_BUFFER, ubo.index, ubo.buf_id)
        yield

    @lru_cache(maxsize=2**6)
    def attribute_location(self, name: str) -> int:
        ' Return the id for the attribute variable `name` or -1 if not found. '
        return glGetAttribLocation(self.program_id, name)

    def __enter__(self):
        glUseProgram(self.program_id)

    def __exit__(self, *args):
        glUseProgram(0)
# }}}
