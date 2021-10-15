import argparse
import json
import threading
import time
from queue import Queue, Empty

import jax
import numpy as np
import optax

from mesh_transformer import util
from mesh_transformer.checkpoint import read_ckpt
from mesh_transformer.sampling import nucleaus_sample
from mesh_transformer.transformer_shard import CausalTransformer
import transformers
from smart_open import open

from mesh_transformer.util import clip_by_global_norm
from flask import Flask, request, make_response, jsonify
app = Flask(__name__)

requests_queue = Queue()

"""
curl --header "Content-Type: application/json" \
  --request POST \
  --data '{"contexts":["src1", "src2"], "targets": [tgt1, tgt2], "top_p": 0.0, "temp": 0.0, "gen_tokens": 64}' \
  http://localhost:5000/complete
"""


def _build_cors_prelight_response():
    response = make_response()
    response.headers.add("Access-Control-Allow-Origin", "*")
    response.headers.add('Access-Control-Allow-Headers', "*")
    response.headers.add('Access-Control-Allow-Methods', "*")
    return response


def _corsify_actual_response(response):
    response.headers.add("Access-Control-Allow-Origin", "*")
    return response


@app.route('/complete', methods=['POST', 'OPTIONS'])
def complete():
    if request.method == "OPTIONS":  # CORS preflight
        return _build_cors_prelight_response()
    elif request.method == "POST":  # The actual request following the preflight
        content = request.json

        if requests_queue.qsize() > 100:
            return {"error": "queue full, try again later"}

        response_queue = Queue()

        requests_queue.put(({
                                "contexts": content["contexts"],
                                "top_p": float(content["top_p"]),
                                "targets": content["targets"],
                                "temp": float(content["temp"]),
                                "gen_tokens": int(content["gen_tokens"])
                            }, response_queue))

        completions = [response_queue.get()]
        while not response_queue.empty():
            completions.append(response_queue.get())

        return _corsify_actual_response(jsonify({"completion": completions}))
    else:
        raise RuntimeError("Weird - don't know how to handle method {}".format(request.method))


def parse_args():
    # Parse command line arguments
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=str, default=None, help="Config file location")

    args = parser.parse_args()
    return args


if __name__ == "__main__":
    threading.Thread(target=app.run, kwargs={"port": 5000, "host": "0.0.0.0"}).start()

    args = parse_args()
    params = json.load(open(args.config))

    gradient_accumulation_steps = params.get("gradient_accumulation_steps", 1)
    per_replica_batch = params["per_replica_batch"]
    cores_per_replica = params["cores_per_replica"]

    assert cores_per_replica <= 8

    bucket = params["bucket"]
    model_dir = params["model_dir"]
    layers = params["layers"]
    d_model = params["d_model"]
    n_heads = params["n_heads"]
    n_vocab = params["n_vocab"]
    seq = params["seq"]
    norm = params["norm"]

    params["sampler"] = nucleaus_sample
    opt = optax.chain(
        optax.scale(1 / gradient_accumulation_steps),
        clip_by_global_norm(1),
        optax.scale_by_adam(),
        optax.additive_weight_decay(0),
        optax.scale(-1),
        optax.scale_by_schedule(util.gpt3_schedule(0, 1, 0, 0))
    )

    params["optimizer"] = opt

    start = time.time()
    print(f"jax devices: {jax.device_count()}")
    print(f"jax runtime initialized in {time.time() - start:.06}s")

    mesh_shape = (jax.device_count() // cores_per_replica, cores_per_replica)
    devices = np.array(jax.devices()).reshape(mesh_shape)

    with open(f"gs://{bucket}/{model_dir}/meta.json", "r") as f:
        meta = json.load(f)

    ckpt_step = meta["checkpoints"][-1]
    print(f"using checkpoint {ckpt_step}")

    total_batch = per_replica_batch * jax.device_count() // cores_per_replica * 8
    with jax.experimental.maps.mesh(devices, ('dp', 'mp')):
        network = CausalTransformer(params)

        start = time.time()
        network.state = read_ckpt(network.state, f"gs://{bucket}/{model_dir}/step_{ckpt_step}/", devices.shape[1])
        print(f"network loaded in {time.time() - start:.06}s")

        local_shards = max(jax.local_device_count() // mesh_shape[1], 1)
        del network.state["opt_state"]
        network.state = network.move_xmap(network.state, np.zeros(local_shards))

        tokenizer = transformers.GPT2TokenizerFast.from_pretrained('gpt2')

        while True:
            all_ctx = []
            try:
                o, q = requests_queue.get(block=False)
                contexts = o["contexts"]
                targets = o["targets"]
                context_length = len(contexts)
                top_p = o["top_p"]
                temp = o["temp"]
                gen_tokens = o["gen_tokens"]

                all_ctx = contexts
                all_top_p = context_length * [top_p]
                all_temp = context_length * [temp]
                all_q = context_length * [q]
            except Empty:
                if len(all_ctx):
                    break
                else:
                    time.sleep(0.01)

            if not all_ctx:
                continue
            start = time.time()
            all_tokenized = []
            all_length = []
            for ctx in all_ctx:
                padded_tokens = np.zeros(seq).astype(np.uint32)
                length = 0

                try:
                    tokens = tokenizer.encode(ctx)
                    provided_ctx = len(tokens)
                    pad_amount = seq - provided_ctx

                    pad_amount = max(pad_amount, 0)

                    padded_tokens = np.pad(tokens, ((pad_amount, 0),)).astype(np.uint32)[-seq:]
                    length = len(tokens)
                except:
                    print("oops exception")

                all_tokenized.append(padded_tokens)
                all_length.append(length)

            output = network.generate(np.array(all_tokenized),
                                      np.array(all_length),
                                      gen_tokens,
                                      {
                                          "top_p": np.array(all_top_p),
                                          "temp": np.array(all_temp)
                                      },
                                      return_logits=True)

            log_probs = np.squeeze(jax.nn.log_softmax(output[1][2], -1))
            indices = output[1][0]
            selected_log_probs = np.squeeze(np.take_along_axis(log_probs, indices, axis=2))

            q = all_q[0]
            response = []
            for o, target in zip(output[1][0][:, :, 0], targets):
                response.append(
                    (
                        tokenizer.convert_ids_to_tokens(o),
                        tokenizer.convert_ids_to_tokens(tokenizer.encode(target))
                     )
                )
            q.put(response)

            print(f"completion done in {time.time() - start:06}s")