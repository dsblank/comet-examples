# Copyright 2019 The TensorFlow Authors. All Rights Reserved.
# Copyright (C) 2021 Comet ML INC
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================


"""
Script to run distributed data parallel training in Tensorflow using MultiWorkerMirroredStrategy

"""
import comet_ml

import os
import argparse
import json
import numpy as np
import tensorflow as tf
import tensorflow.keras as keras

PROJECT_NAME = "tf-distributed-multiworker-mirrored"

os.environ["GRPC_FAIL_FAST"] = "use_caller"

fashion_mnist = tf.keras.datasets.fashion_mnist
(train_images, train_labels), (test_images, test_labels) = fashion_mnist.load_data()

train_images = train_images[..., None]
test_images = test_images[..., None]

train_images = train_images / np.float32(255)
test_images = test_images / np.float32(255)

BUFFER_SIZE = len(train_images)

EPOCHS = 10
BATCH_SIZE_PER_REPLICA = 64


def build_model():
    model = tf.keras.Sequential(
        [
            tf.keras.layers.Conv2D(32, 3, activation="relu"),
            tf.keras.layers.MaxPooling2D(),
            tf.keras.layers.Conv2D(64, 3, activation="relu"),
            tf.keras.layers.MaxPooling2D(),
            tf.keras.layers.Flatten(),
            tf.keras.layers.Dense(64, activation="relu"),
            tf.keras.layers.Dense(10),
        ]
    )

    return model


def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run_id", type=int)
    parser.add_argument("--worker_hosts")
    parser.add_argument("--task_index", type=int)

    return parser.parse_args()


def main():
    args = get_args()

    run_id = args.run_id
    task_index = args.task_index

    worker_hosts = args.worker_hosts.split(",")
    num_workers = len(worker_hosts)

    cluster_dict = {
        "cluster": {"worker": worker_hosts},
        "task": {"type": "worker", "index": task_index},
    }
    os.environ["TF_CONFIG"] = json.dumps(cluster_dict)
    if task_index == 0:
        experiment = comet_ml.Experiment(project_name=PROJECT_NAME)
        experiment.log_other("run_id", run_id)
        experiment.log_other("worker_hosts", args.worker_hosts)

    strategy = tf.distribute.MultiWorkerMirroredStrategy()
    GLOBAL_BATCH_SIZE = BATCH_SIZE_PER_REPLICA * strategy.num_replicas_in_sync

    with strategy.scope():
        model = build_model()

        optimizer = tf.keras.optimizers.Adam()
        criterion = tf.keras.losses.SparseCategoricalCrossentropy(
            from_logits=True, reduction=tf.keras.losses.Reduction.NONE
        )
        train_accuracy = tf.keras.metrics.SparseCategoricalAccuracy(
            name="train_accuracy"
        )

        def compute_loss(labels, predictions):
            per_example_loss = criterion(labels, predictions)
            return tf.nn.compute_average_loss(per_example_loss, global_batch_size=GLOBAL_BATCH_SIZE)
        
    def step_fn(dataset_inputs):
        def train_step(inputs):
            images, labels = inputs
            with tf.GradientTape() as tape:
                pred = model(images, training=True)
                loss = compute_loss(labels, pred)
                gradients = tape.gradient(loss, model.trainable_variables)
            optimizer.apply_gradients(zip(gradients, model.trainable_variables))
            actual_pred = tf.cast(tf.greater(pred, 0.5), tf.int64)
            train_accuracy.update_state(labels, actual_pred)
            return loss
        losses = strategy.run(train_step, args=(dataset_inputs,))
        return strategy.reduce(tf.distribute.ReduceOp.SUM, losses, axis=None)
    
    def dataset_fn(_):
        train_dataset = (
            tf.data.Dataset.from_tensor_slices((train_images, train_labels))
            .shuffle(BUFFER_SIZE)
            .batch(GLOBAL_BATCH_SIZE)
        )
        return train_dataset

    dist_dataset = strategy.distribute_datasets_from_function(dataset_fn)
    for i in range(EPOCHS):
        total_loss = 0
        num_batches = 0
        for x in dist_dataset:
            loss = step_fn(x)
            total_loss += loss
            num_batches += 1
        if task_index == 0:
            experiment.log_metric("train_loss", total_loss / num_batches, epoch=i)
        # Wait at epoch boundaries.
        print(
            "Finished epoch %d, accuracy is %f." % (i, train_accuracy.result().numpy())
        )
        train_accuracy.reset_states()


if __name__ == "__main__":
    main()
