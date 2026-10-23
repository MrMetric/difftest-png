CFLAGS ?= -O2 -Wall -Wextra
shim: shim.c
	$(CC) $(CFLAGS) $(shell pkg-config --cflags spng) -o $@ $< $(shell pkg-config --libs spng)
clean:
	rm -f shim
