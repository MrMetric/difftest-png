CFLAGS ?= -O2 -Wall -Wextra -Wno-unused-function
STB_VER := $(shell grep -m1 -oE 'stb_image - v[0-9.]+' stb_image.h | sed 's/stb_image - //')
shim: shim.c stb_image.h
	$(CC) $(CFLAGS) -DSTB_VER='"$(STB_VER)"' -o $@ $< -lm
clean:
	rm -f shim
