# Copyright 2022 The KerasCV Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Title: Train an Object Detection Model on Pascal VOC 2007 using KerasCV
Author: [lukewood](https://github.com/LukeWood), [tanzhenyu](https://github.com/tanzhenyu)
Date created: 2022/09/27
Last modified: 2022/12/08
Description: Use KerasCV to train a RetinaNet on Pascal VOC 2007.
"""
import resource
import sys

import tensorflow as tf
import tensorflow_datasets as tfds
from absl import flags
from tensorflow import keras

import keras_cv
from keras_cv import layers
from keras_cv.callbacks import PyCOCOCallback

low, high = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, (high, high))

EPOCHS = 100
CHECKPOINT_PATH = "checkpoint/"

flags.DEFINE_string(
    "weights_name",
    "weights_{epoch:02d}.h5",
    "Directory which will be used to store weight checkpoints.",
)
flags.DEFINE_string(
    "tensorboard_path",
    "logs",
    "Directory which will be used to store tensorboard logs.",
)
FLAGS = flags.FLAGS
FLAGS(sys.argv)


# Try to detect an available TPU. If none is present, default to MirroredStrategy
try:
    tpu = tf.distribute.cluster_resolver.TPUClusterResolver.connect()
    strategy = tf.distribute.TPUStrategy(tpu)
except ValueError:
    # MirroredStrategy is best for a single machine with one or multiple GPUs
    strategy = tf.distribute.MirroredStrategy()

BATCH_SIZE = 4
GLOBAL_BATCH_SIZE = BATCH_SIZE * strategy.num_replicas_in_sync
BASE_LR = 0.01 * GLOBAL_BATCH_SIZE / 16
print("Number of accelerators: ", strategy.num_replicas_in_sync)
print("Global Batch Size: ", GLOBAL_BATCH_SIZE)

IMG_SIZE = 640
image_size = [IMG_SIZE, IMG_SIZE, 3]
train_ds = tfds.load(
    "voc/2007", split="train+validation", with_info=False, shuffle_files=True
)
train_ds = train_ds.concatenate(
    tfds.load("voc/2012", split="train+validation", with_info=False, shuffle_files=True)
)
eval_ds = tfds.load("voc/2007", split="test", with_info=False)


def unpackage_inputs(bounding_box_format):
    def apply(inputs):
        image = inputs["image"]
        image = tf.cast(image, tf.float32)
        image = tf.keras.applications.resnet50.preprocess_input(image)
        gt_boxes = tf.cast(inputs["objects"]["bbox"], tf.float32)
        gt_classes = tf.cast(inputs["objects"]["label"], tf.float32)
        gt_classes = tf.expand_dims(gt_classes, axis=1)
        gt_boxes = keras_cv.bounding_box.convert_format(
            gt_boxes,
            images=image,
            source="rel_yxyx",
            target=bounding_box_format,
        )
        bounding_boxes = tf.concat([gt_boxes, gt_classes], axis=-1)
        return {"images": image, "bounding_boxes": bounding_boxes}

    return apply


train_ds = train_ds.map(unpackage_inputs("xywh"), num_parallel_calls=tf.data.AUTOTUNE)
train_ds = train_ds.apply(
    tf.data.experimental.dense_to_ragged_batch(GLOBAL_BATCH_SIZE, drop_remainder=True)
)

train_ds = train_ds.shuffle(8 * strategy.num_replicas_in_sync)
train_ds = train_ds.prefetch(tf.data.AUTOTUNE)

eval_ds = eval_ds.map(
    unpackage_inputs("xywh"),
    num_parallel_calls=tf.data.AUTOTUNE,
)
eval_ds = eval_ds.apply(
    tf.data.experimental.dense_to_ragged_batch(GLOBAL_BATCH_SIZE, drop_remainder=True)
)
eval_ds = eval_ds.prefetch(tf.data.AUTOTUNE)


"""
Our data pipeline is now complete.  We can now move on to data augmentation:
"""
eval_resizing = layers.Resizing(
    IMG_SIZE, IMG_SIZE, bounding_box_format="xywh", pad_to_aspect_ratio=True
)

augmenter = layers.Augmenter(
    [
        layers.RandomFlip(mode="horizontal", bounding_box_format="xywh"),
        layers.JitteredResize(
            target_size=(IMG_SIZE, IMG_SIZE),
            scale_factor=(0.8, 1.25),
            bounding_box_format="xywh",
        ),
        layers.MaybeApply(layers.MixUp(), rate=0.5, batchwise=True),
    ]
)

train_ds = train_ds.map(augmenter, num_parallel_calls=tf.data.AUTOTUNE)
eval_ds = eval_ds.map(
    eval_resizing,
    num_parallel_calls=tf.data.AUTOTUNE,
)
"""
## Model creation

We'll use the KerasCV API to construct a RetinaNet model.  In this tutorial we use
a pretrained ResNet50 backbone using weights.  In order to perform fine-tuning, we
freeze the backbone before training.  When `include_rescaling=True` is set, inputs to
the model are expected to be in the range `[0, 255]`.
"""


def unpackage_inputs(data):
    return data["images"], data["bounding_boxes"]


train_ds = train_ds.map(unpackage_inputs, num_parallel_calls=tf.data.AUTOTUNE)
eval_ds = eval_ds.map(unpackage_inputs, num_parallel_calls=tf.data.AUTOTUNE)


# TODO(lukewood): the boxes loses shape from KPL, so need to pad to a known shape.
# TODO(tanzhenyu): consider remove padding while reduce function tracing.
def pad_fn(image, boxes):
    boxes = boxes.to_tensor(default_value=-1.0, shape=[GLOBAL_BATCH_SIZE, 32, 5])
    gt_boxes = boxes[..., :4]
    gt_classes = boxes[..., 4]
    return image, {
        "boxes": gt_boxes,
        "classes": gt_classes,
    }


train_ds = train_ds.map(pad_fn, num_parallel_calls=tf.data.AUTOTUNE)
eval_ds = eval_ds.map(pad_fn, num_parallel_calls=tf.data.AUTOTUNE)

with strategy.scope():
    inputs = keras.layers.Input(shape=image_size)
    x = inputs
    x = keras.applications.resnet.preprocess_input(x)

    backbone = keras.applications.ResNet50(
        include_top=False, input_tensor=x, weights="imagenet"
    )

    c3_output, c4_output, c5_output = [
        backbone.get_layer(layer_name).output
        for layer_name in ["conv3_block4_out", "conv4_block6_out", "conv5_block3_out"]
    ]
    backbone = keras.Model(inputs=inputs, outputs=[c3_output, c4_output, c5_output])
    # keras_cv backbone gives 2mAP lower result.
    # TODO(ian): should eventually use keras_cv backbone.
    # backbone = keras_cv.models.ResNet50(
    #     include_top=False, weights="imagenet", include_rescaling=False
    # ).as_backbone()
    model = keras_cv.models.RetinaNet(
        # number of classes to be used in box classification
        classes=20,
        # For more info on supported bounding box formats, visit
        # https://keras.io/api/keras_cv/bounding_box/
        bounding_box_format="xywh",
        backbone=backbone,
    )
    # Fine-tuning a RetinaNet is as simple as setting backbone.trainable = False
    model.backbone.trainable = False
    optimizer = tf.optimizers.SGD(learning_rate=BASE_LR, global_clipnorm=10.0)

model.compile(
    classification_loss="focal",
    box_loss="smoothl1",
    optimizer=optimizer,
)

callbacks = [
    keras.callbacks.TensorBoard(log_dir="logs"),
    keras.callbacks.ReduceLROnPlateau(patience=5),
    keras.callbacks.EarlyStopping(patience=10),
    keras.callbacks.ModelCheckpoint(CHECKPOINT_PATH, save_weights_only=True),
    PyCOCOCallback(eval_ds, "xywh"),
]

history = model.fit(
    train_ds,
    validation_data=eval_ds,
    epochs=35,
    callbacks=callbacks,
)
