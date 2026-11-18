#include "ap_axi_sdata.h"
#include "hls_stream.h"
#include "cnn.h"

// one ap_fixed<16,6> per beat -> 16-bit AXIS (a DMA-legal width)
typedef ap_axiu<16, 0, 0, 0> axis_t;

#define N_T   150   // timesteps = model window (was 100)
#define N_C   6     // channels per timestep
#define N_OUT 8     // classes

void cnn_axi(hls::stream<axis_t> &in, hls::stream<axis_t> &out) {
    #pragma HLS INTERFACE axis port=in
    #pragma HLS INTERFACE axis port=out
    //#pragma HLS INTERFACE ap_ctrl_none port=return
    #pragma HLS INTERFACE s_axilite port=return bundle=control
    // NO DATAFLOW: run the 3 stages sequentially to avoid the
    // ap_ctrl_none + auto-rewind-pipeline deadlock (HLS 200-656).

    hls::stream<input_t>  in_nn("in_nn");
    hls::stream<result_t> out_nn("out_nn");
    #pragma HLS STREAM variable=in_nn  depth=256   // holds all 150 timesteps
    #pragma HLS STREAM variable=out_nn depth=8

    // ---- stage 1: unpack 900 beats -> 150 timesteps of 6 channels ----
    for (int t = 0; t < N_T; t++) {
        input_t v;
        for (int c = 0; c < N_C; c++) {
            #pragma HLS PIPELINE
            axis_t beat = in.read();
            ap_fixed<16,6> x;
            x.range(15,0) = beat.data.range(15,0);
            v[c] = x;
        }
        in_nn.write(v);
    }

    // ---- stage 2: run the hls4ml network ----
    cnn(in_nn, out_nn);

    // ---- stage 3: emit 8 beats, TLAST on the last ----
    result_t r = out_nn.read();
    for (int k = 0; k < N_OUT; k++) {
        #pragma HLS PIPELINE
        axis_t beat;
        ap_fixed<16,6> y = r[k];              // result_t is now <16,6>, so this is a no-op cast
        beat.data.range(15,0) = y.range(15,0);
        beat.keep = -1;
        beat.strb = -1;
        beat.last = (k == N_OUT - 1);
        out.write(beat);
    }
}
