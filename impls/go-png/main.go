// Go image/png shim: decode one PNG with the standard library and emit PAM.
//
// Policy: whatever concrete image type image/png returns is written in its
// native layout (Gray, Gray16, NRGBA, NRGBA64, RGBA/RGBA64 which image/png
// only produces for opaque images, Paletted expanded through its palette).
package main

import (
	"bufio"
	"fmt"
	"image"
	"image/color"
	"image/png"
	"os"
	"runtime"
)

func fail(msg string) {
	fmt.Printf("ERR %s\n", msg)
	os.Exit(1)
}

func main() {
	if len(os.Args) == 2 && os.Args[1] == "--version" {
		fmt.Printf("go image/png %s\n", runtime.Version())
		return
	}
	if len(os.Args) != 3 {
		fmt.Fprintln(os.Stderr, "usage: shim in.png out.pam")
		os.Exit(2)
	}
	f, err := os.Open(os.Args[1])
	if err != nil {
		fail(err.Error())
	}
	cfg, err := png.DecodeConfig(bufio.NewReader(f))
	if err == nil {
		fmt.Printf("INFO config width=%d height=%d model=%T\n", cfg.Width, cfg.Height, cfg.ColorModel)
	}
	f.Seek(0, 0)
	img, err := png.Decode(bufio.NewReader(f))
	if err != nil {
		fail(err.Error())
	}
	b := img.Bounds()
	w, h := b.Dx(), b.Dy()
	var channels, depth int
	var body []byte
	switch m := img.(type) {
	case *image.Gray:
		channels, depth, body = 1, 8, rows(m.Pix, m.Stride, w*1, h)
	case *image.Gray16:
		channels, depth, body = 1, 16, rows(m.Pix, m.Stride, w*2, h)
	case *image.NRGBA:
		channels, depth, body = 4, 8, rows(m.Pix, m.Stride, w*4, h)
	case *image.NRGBA64:
		channels, depth, body = 4, 16, rows(m.Pix, m.Stride, w*8, h)
	case *image.RGBA:
		channels, depth, body = 4, 8, rows(m.Pix, m.Stride, w*4, h)
	case *image.RGBA64:
		channels, depth, body = 4, 16, rows(m.Pix, m.Stride, w*8, h)
	case *image.Paletted:
		channels, depth = 4, 8
		body = make([]byte, 0, w*h*4)
		for y := 0; y < h; y++ {
			for x := 0; x < w; x++ {
				idx := int(m.Pix[y*m.Stride+x])
				if idx >= len(m.Palette) {
					fail(fmt.Sprintf("palette index %d outside palette of %d", idx, len(m.Palette)))
				}
				c := color.NRGBAModel.Convert(m.Palette[idx]).(color.NRGBA)
				body = append(body, c.R, c.G, c.B, c.A)
			}
		}
	default:
		fail(fmt.Sprintf("unhandled image type %T", img))
	}
	fmt.Printf("INFO type=%T\n", img)
	tt := map[int]string{1: "GRAYSCALE", 2: "GRAYSCALE_ALPHA", 3: "RGB", 4: "RGB_ALPHA"}[channels]
	maxval := 255
	if depth == 16 {
		maxval = 65535
	}
	out, err := os.Create(os.Args[2])
	if err != nil {
		fail(err.Error())
	}
	bw := bufio.NewWriterSize(out, 1<<20)
	fmt.Fprintf(bw, "P7\nWIDTH %d\nHEIGHT %d\nDEPTH %d\nMAXVAL %d\nTUPLTYPE %s\nENDHDR\n", w, h, channels, maxval, tt)
	bw.Write(body)
	if err := bw.Flush(); err != nil {
		fail(err.Error())
	}
	out.Close()
	fmt.Printf("OK %d %d %d %d\n", w, h, channels, depth)
}

func rows(pix []byte, stride, rowBytes, h int) []byte {
	if stride == rowBytes {
		return pix[:rowBytes*h]
	}
	out := make([]byte, 0, rowBytes*h)
	for y := 0; y < h; y++ {
		out = append(out, pix[y*stride:y*stride+rowBytes]...)
	}
	return out
}
