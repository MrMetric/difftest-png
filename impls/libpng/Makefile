CFLAGS ?= -O2 -Wall -Wextra
shim: shim.c
	$(CC) $(CFLAGS) $(shell pkg-config --cflags libpng) -o $@ $< $(shell pkg-config --libs libpng)
clean:
	rm -f shim
