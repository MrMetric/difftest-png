//! Rust `png` crate shim: decode one PNG with the crate's defaults and emit PAM.
//!
//! Policy: `Transformations::EXPAND` only (palette -> RGB(A), tRNS -> alpha,
//! low bit depths -> 8). No 16->8 stripping, no gamma. CRC/Adler policy and
//! memory limits are the crate's defaults, measured rather than configured.
//! 16-bit samples are already big-endian in the crate's output buffer.
use std::fs::File;
use std::io::{BufReader, BufWriter, Write};

fn fail(msg: &str) -> ! {
	println!("ERR {msg}");
	std::process::exit(1);
}

fn main() {
	let args: Vec<String> = std::env::args().collect();
	if args.len() != 3 {
		eprintln!("usage: rust-png-shim in.png out.pam");
		std::process::exit(2);
	}
	let f = File::open(&args[1]).unwrap_or_else(|e| fail(&format!("open: {e}")));
	let mut dec = png::Decoder::new(BufReader::new(f));
	dec.set_transformations(png::Transformations::EXPAND);
	let mut reader = dec.read_info().unwrap_or_else(|e| fail(&e.to_string()));
	{
		let info = reader.info();
		println!(
			"INFO ihdr width={} height={} bit_depth={} color_type={} interlaced={}",
			info.width, info.height, info.bit_depth as u8, info.color_type as u8, info.interlaced
		);
		let mut chunks: Vec<&str> = Vec::new();
		if info.palette.is_some() { chunks.push("PLTE"); }
		if info.trns.is_some() { chunks.push("tRNS"); }
		if info.gama_chunk.is_some() { chunks.push("gAMA"); }
		if info.chrm_chunk.is_some() { chunks.push("cHRM"); }
		if info.srgb.is_some() { chunks.push("sRGB"); }
		if info.icc_profile.is_some() { chunks.push("iCCP"); }
		if info.sbit.is_some() { chunks.push("sBIT"); }
		if info.pixel_dims.is_some() { chunks.push("pHYs"); }
		if info.animation_control.is_some() { chunks.push("acTL"); }
		println!(
			"INFO chunks={} text={}",
			chunks.join(","),
			info.uncompressed_latin1_text.len() + info.compressed_latin1_text.len() + info.utf8_text.len()
		);
	}
	let size = reader.output_buffer_size().unwrap_or_else(|| fail("output buffer size overflow"));
	let mut buf: Vec<u8> = Vec::new();
	buf.try_reserve_exact(size).unwrap_or_else(|_| fail(&format!("out of memory: output buffer of {size} bytes")));
	buf.resize(size, 0);
	let out = reader.next_frame(&mut buf).unwrap_or_else(|e| fail(&e.to_string()));
	let (w, h) = (out.width, out.height);
	let channels = out.color_type.samples();
	let depth = out.bit_depth as u8;
	let maxval = match depth {
		8 => 255,
		16 => 65535,
		d => fail(&format!("unexpected output bit depth {d}")),
	};
	let tt = match channels {
		1 => "GRAYSCALE",
		2 => "GRAYSCALE_ALPHA",
		3 => "RGB",
		4 => "RGB_ALPHA",
		n => fail(&format!("unexpected channel count {n}")),
	};
	let n = out.buffer_size();
	let mut o = BufWriter::new(File::create(&args[2]).unwrap_or_else(|e| fail(&format!("create: {e}"))));
	write!(o, "P7\nWIDTH {w}\nHEIGHT {h}\nDEPTH {channels}\nMAXVAL {maxval}\nTUPLTYPE {tt}\nENDHDR\n")
		.and_then(|_| o.write_all(&buf[..n]))
		.and_then(|_| o.flush())
		.unwrap_or_else(|e| fail(&format!("write: {e}")));
	println!("OK {w} {h} {channels} {depth}");
}
