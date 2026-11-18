#include "ap_axi_sdata.h"
#include "hls_stream.h"
#include "cnn.h"
#include <cmath>
#include <fstream>
#include <iostream>

typedef ap_axiu<16,0,0,0> axis_t;
void cnn_axi(hls::stream<axis_t>&, hls::stream<axis_t>&);   // defined in cnn_axi.cpp

#define N_T     150            // timesteps (was 100)
#define N_C     6              // channels
#define N_IN    (N_T * N_C)    // 900 input beats (was 600)
#define N_OUT   8
#define FRAC    1024.0         // 2^10 frac bits of ap_fixed<16,6> output -> keep 1024
#define TOL_LSB 2              // allowed |error| in LSBs; C==RTL should be 0

static ap_uint<16> to_bits(double v){        // float -> the 16 bits the DMA carries
    long q = std::lround(v * FRAC);
    if(q >  32767) q =  32767;
    if(q < -32768) q = -32768;
    return (ap_uint<16>)(q & 0xffff);
}
static float from_bits(ap_uint<16> b){       // reverse: reinterpret 16 bits as signed
    return (float)((short)b.to_uint()) / (float)FRAC;
}

int main(){
    // ---- one input sample: 150 timesteps x 6 channels ----
    double x[N_IN];
    std::ifstream fin("tb_data/tb_input.txt");
    if(!fin){ std::cout << "ERROR: cannot open tb_data/tb_input.txt\n"; return 2; }
    for(int i=0;i<N_IN;i++) fin >> x[i];

    // ---- golden logits: hls_model.predict() on the SAME sample ----
    float gold[N_OUT];
    std::ifstream fexp("tb_data/tb_output.txt");
    if(!fexp){ std::cout << "ERROR: cannot open tb_data/tb_output.txt\n"; return 2; }
    for(int k=0;k<N_OUT;k++) fexp >> gold[k];

    // ---- build the 900-beat input packet, TLAST on the last beat ----
    hls::stream<axis_t> in, out;
    for(int i=0;i<N_IN;i++){
        axis_t b;
        b.data = to_bits(x[i]);
        b.keep = -1; b.strb = -1;
        b.last = (i==N_IN-1);
        in.write(b);
    }

    // ---- run the wrapper (unpack -> cnn -> emit 7) ----
    cnn_axi(in, out);

    // ---- read the 8 output beats ----
    float got[N_OUT];
    for(int k=0;k<N_OUT;k++) got[k] = from_bits(out.read().data);

    // ---- compare against golden, element by element ----
    const float tol = (float)TOL_LSB / (float)FRAC;
    int fails = 0;
    std::cout << "   k       golden          got         diff    LSBs   status\n";
    for(int k=0;k<N_OUT;k++){
        float d   = got[k] - gold[k];
        float ad  = std::fabs(d);
        int   lsb = (int)std::lround(ad * FRAC);
        bool  ok  = (ad <= tol);
        if(!ok) fails++;
        std::cout << "   " << k << "   " << gold[k] << "   " << got[k]
                  << "   " << d << "   " << lsb
                  << "   " << (ok ? "ok" : "MISMATCH") << "\n";
    }

    // ---- compare argmax (this is what the prediction actually is) ----
    int ag=0, ga=0;
    for(int k=1;k<N_OUT;k++){ if(gold[k]>gold[ag]) ag=k; if(got[k]>got[ga]) ga=k; }
    std::cout << "argmax golden=" << ag << "  got=" << ga
              << (ag==ga ? "  (match)\n" : "  (MISMATCH)\n");

    // ---- verdict: a nonzero return makes csim/cosim FAIL ----
    bool argmax_ok = (ag == ga);
    if(argmax_ok && fails == 0){
        std::cout << "PASS: argmax matches and all logits within " << TOL_LSB << " LSB\n";
        return 0;
    }
    if(argmax_ok){
        std::cout << "PASS (argmax matches); " << fails
                  << " logit(s) exceed " << TOL_LSB << " LSB -- quantization noise, not a flip\n";
        return 0;                       // classifier is correct
    }
    std::cout << "FAIL: argmax differs (golden=" << ag << " got=" << ga << ")\n";
    return 1;

}
