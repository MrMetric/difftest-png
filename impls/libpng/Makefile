CFLAGS ?= -O2 -Wall -Wextra
shim: shim.c ../pam_rows.h
	$(CC) $(CFLAGS) $(shell pkg-config --cflags libpng) -o $@ $< $(shell pkg-config --libs libpng)
clean:
	rm -f shim
