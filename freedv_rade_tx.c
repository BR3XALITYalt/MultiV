#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>

#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#endif

#include "arch.h"
#include "cpu_support.h"
#include "lpcnet.h"
#include "rade_api.h"
#include "rade_dsp.h"

static int write_audio_block(const RADE_COMP *iq, int count) {
    uint32_t sample_count = (uint32_t)count;

    if (count <= 0 || fwrite(&sample_count, sizeof(sample_count), 1, stdout) != 1) {
        return -1;
    }

    for (int i = 0; i < count; ++i) {
        /* Match FreeDV's WAV output scaling and retain 6 dB of peak headroom. */
        float sample = iq[i].real * (RADE_INT16_SCALE / 32768.0f);
        if (!isfinite(sample)) {
            sample = 0.0f;
        }
        if (fwrite(&sample, sizeof(sample), 1, stdout) != 1) {
            return -1;
        }
    }

    return fflush(stdout) == 0 ? 0 : -1;
}

int main(void) {
#ifdef _WIN32
    _setmode(_fileno(stdin), O_BINARY);
    _setmode(_fileno(stdout), O_BINARY);
#endif

    int arch = opus_select_arch();
    LPCNetEncState *speech_encoder = lpcnet_encoder_create();
    if (speech_encoder == NULL) {
        fprintf(stderr, "Could not initialize the FreeDV speech feature encoder.\n");
        return 1;
    }

    rade_initialize();
    struct rade *modem = rade_open("", RADE_USE_C_ENCODER | RADE_VERBOSE_0);
    if (modem == NULL) {
        fprintf(stderr, "Could not initialize the FreeDV RADE V1 modem.\n");
        lpcnet_encoder_destroy(speech_encoder);
        rade_finalize();
        return 1;
    }

    const int feature_count = rade_n_features_in_out(modem);
    const int modem_sample_count = rade_n_tx_out(modem);
    const int end_of_over_count = rade_n_tx_eoo_out(modem);
    const int feature_frames_per_modem = feature_count / RADE_NB_TOTAL_FEATURES;

    if (feature_count <= 0 ||
        feature_frames_per_modem <= 0 ||
        feature_frames_per_modem * RADE_NB_TOTAL_FEATURES != feature_count ||
        modem_sample_count <= 0 ||
        end_of_over_count <= 0) {
        fprintf(stderr, "The FreeDV RADE V1 modem reported invalid frame dimensions.\n");
        rade_close(modem);
        rade_finalize();
        lpcnet_encoder_destroy(speech_encoder);
        return 1;
    }

    float *features = (float *)calloc((size_t)feature_count, sizeof(float));
    RADE_COMP *modem_output = (RADE_COMP *)malloc(
        (size_t)modem_sample_count * sizeof(RADE_COMP)
    );
    RADE_COMP *end_of_over_output = (RADE_COMP *)malloc(
        (size_t)end_of_over_count * sizeof(RADE_COMP)
    );

    if (features == NULL || modem_output == NULL || end_of_over_output == NULL) {
        fprintf(stderr, "Could not allocate FreeDV RADE frame buffers.\n");
        free(features);
        free(modem_output);
        free(end_of_over_output);
        rade_close(modem);
        rade_finalize();
        lpcnet_encoder_destroy(speech_encoder);
        return 1;
    }

    opus_int16 speech_frame[LPCNET_FRAME_SIZE];
    int feature_frame_index = 0;
    int result = 0;

    while (1) {
        size_t samples_read = fread(
            speech_frame,
            sizeof(opus_int16),
            LPCNET_FRAME_SIZE,
            stdin
        );

        if (samples_read == 0) {
            break;
        }

        if (samples_read < LPCNET_FRAME_SIZE) {
            memset(
                speech_frame + samples_read,
                0,
                (LPCNET_FRAME_SIZE - samples_read) * sizeof(opus_int16)
            );
        }

        lpcnet_compute_single_frame_features(
            speech_encoder,
            speech_frame,
            &features[feature_frame_index * RADE_NB_TOTAL_FEATURES],
            arch
        );
        ++feature_frame_index;

        if (feature_frame_index == feature_frames_per_modem) {
            int output_count = rade_tx(modem, modem_output, features);
            if (write_audio_block(modem_output, output_count) != 0) {
                result = 1;
                break;
            }
            memset(features, 0, (size_t)feature_count * sizeof(float));
            feature_frame_index = 0;
        }

        if (samples_read < LPCNET_FRAME_SIZE) {
            break;
        }
    }

    if (result == 0 && feature_frame_index > 0) {
        int output_count;
        memset(
            &features[feature_frame_index * RADE_NB_TOTAL_FEATURES],
            0,
            (size_t)(feature_frames_per_modem - feature_frame_index)
                * RADE_NB_TOTAL_FEATURES * sizeof(float)
        );
        output_count = rade_tx(modem, modem_output, features);
        if (write_audio_block(modem_output, output_count) != 0) {
            result = 1;
        }
    }

    if (result == 0) {
        int output_count = rade_tx_eoo(modem, end_of_over_output);
        if (write_audio_block(end_of_over_output, output_count) != 0) {
            result = 1;
        }
    }

    free(features);
    free(modem_output);
    free(end_of_over_output);
    rade_close(modem);
    rade_finalize();
    lpcnet_encoder_destroy(speech_encoder);
    return result;
}
