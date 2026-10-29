// image/png's native image, serialized by row. Only palette output needs expansion.
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

func fail(msg string)     { fmt.Printf("ERR %s\n", msg); os.Exit(1) }
func shimFail(msg string) { fmt.Printf("SHIM %s\n", msg); os.Exit(70) }

// The library returned without error but not an image the shim can write: its result.
func apiFail(msg string) { fmt.Printf("API %s\n", msg); os.Exit(71) }
func returned(ok bool, msg string) {
	if !ok {
		apiFail(msg)
	}
}
func require(ok bool, msg string) {
	if !ok {
		shimFail(msg)
	}
}

func main() {
	if len(os.Args) == 2 && os.Args[1] == "--version" {
		fmt.Printf("go image/png %s\n", runtime.Version())
		return
	}
	require(len(os.Args) == 3, "usage: shim in.png out.pam")
	f, err := os.Open(os.Args[1])
	if err != nil {
		shimFail(err.Error())
	}
	img, err := png.Decode(bufio.NewReader(f))
	f.Close()
	if err != nil {
		fail(err.Error())
	}
	b := img.Bounds()
	w, h := b.Dx(), b.Dy()
	var channels, depth, stride int
	var pix []byte
	var palette *image.Paletted
	switch m := img.(type) {
	case *image.Gray:
		channels, depth, pix, stride = 1, 8, m.Pix, m.Stride
	case *image.Gray16:
		channels, depth, pix, stride = 1, 16, m.Pix, m.Stride
	case *image.NRGBA:
		channels, depth, pix, stride = 4, 8, m.Pix, m.Stride
	case *image.NRGBA64:
		channels, depth, pix, stride = 4, 16, m.Pix, m.Stride
	case *image.RGBA:
		returned(m.Opaque(), "premultiplied RGBA returned for a transparent PNG")
		channels, depth, pix, stride = 4, 8, m.Pix, m.Stride
	case *image.RGBA64:
		returned(m.Opaque(), "premultiplied RGBA64 returned for a transparent PNG")
		channels, depth, pix, stride = 4, 16, m.Pix, m.Stride
	case *image.Paletted:
		channels, depth, palette = 4, 8, m
	default:
		apiFail(fmt.Sprintf("unhandled image type %T", img))
	}
	returned(w > 0 && h > 0 && b.Min.X == 0 && b.Min.Y == 0, "unexpected decoded bounds")
	fmt.Printf("INFO type=%T\n", img)
	tt := []string{"GRAYSCALE", "GRAYSCALE_ALPHA", "RGB", "RGB_ALPHA"}[channels-1]
	maxval := 255
	if depth == 16 {
		maxval = 65535
	}
	out, err := os.Create(os.Args[2])
	if err != nil {
		shimFail(err.Error())
	}
	bw := bufio.NewWriter(out)
	if _, err := fmt.Fprintf(bw, "P7\nWIDTH %d\nHEIGHT %d\nDEPTH %d\nMAXVAL %d\nTUPLTYPE %s\nENDHDR\n", w, h, channels, maxval, tt); err != nil {
		shimFail(err.Error())
	}
	rowBytes := w * channels * (depth / 8)
	var expanded []byte
	if palette != nil {
		expanded = make([]byte, rowBytes)
	} else {
		returned(stride >= rowBytes && len(pix) >= (h-1)*stride+rowBytes, "short native pixel buffer")
	}
	for y := 0; y < h; y++ {
		var row []byte
		if palette == nil {
			row = pix[y*stride : y*stride+rowBytes]
		} else {
			for x := 0; x < w; x++ {
				idx := int(palette.Pix[y*palette.Stride+x])
				if idx >= len(palette.Palette) {
					apiFail(fmt.Sprintf("palette index %d outside palette of %d", idx, len(palette.Palette)))
				}
				c := color.NRGBAModel.Convert(palette.Palette[idx]).(color.NRGBA)
				expanded[4*x], expanded[4*x+1], expanded[4*x+2], expanded[4*x+3] = c.R, c.G, c.B, c.A
			}
			row = expanded
		}
		if _, err := bw.Write(row); err != nil {
			shimFail(err.Error())
		}
	}
	if err := bw.Flush(); err != nil {
		shimFail(err.Error())
	}
	if err := out.Close(); err != nil {
		shimFail(err.Error())
	}
	fmt.Printf("OK %d %d %d %d\n", w, h, channels, depth)
}
