import tensorflow as tf
from finetune.base_models.gpt.featurizer import dropout, embed


def textcnn_featurizer(X, encoder, config, train=False, reuse=None):
    """
    The transformer element of the finetuning model. Maps from tokens ids to a dense, embedding of the sequence.

    :param X: A tensor of token indexes with shape [batch_size, sequence_length, token_idx]
    :param encoder: A TextEncoder object.
    :param config: A config object, containing all parameters for the featurizer.
    :param train: If this flag is true, dropout and losses are added to the graph.
    :param reuse: Should reuse be set within this scope.
    :return: A dict containing;
        embed_weights: the word embedding matrix.
        features: The output of the featurizer_final state.
        sequence_features: The output of the featurizer at each timestep.
    """
    initial_shape = [a or -1 for a in X.get_shape().as_list()]
    X = tf.reshape(X, shape=[-1] + initial_shape[-2:])

    with tf.variable_scope('model/featurizer', reuse=reuse):
        embed_weights = tf.get_variable(
            name="we",
            shape=[encoder.vocab_size + config.max_length, config.n_embed_featurizer],
            initializer=tf.random_normal_initializer(stddev=config.weight_stddev)
        )
        if config.train_embeddings:
            embed_weights = dropout(embed_weights, config.embed_p_drop, train)
        else:
            embed_weights = tf.stop_gradient(embed_weights)

        X = tf.reshape(X, [-1, config.max_length, 2])

        h = embed(X, embed_weights)

        # we use the first transformer block of GPT as our embedding layer
        # layer = 0
        # with tf.variable_scope('h%d_' % layer):
        #     block_fn = functools.partial(block, n_head=config.n_heads, act_fn=config.act_fn,
        #                                  resid_pdrop=config.resid_p_drop, attn_pdrop=config.attn_p_drop,
        #                                  scope='h%d' % layer, train=train, scale=True)
        #     if config.low_memory_mode and train:
        #         block_fn = recompute_grad(block_fn, use_entire_scope=True)
        #     h = block_fn(h)

        # # Use hidden state at classifier token as input to final proj. + softmax
        # # Note: we get seq_feats and pool_idx from the output of the transformer block before the convolutional layer
        # clf_token = encoder['_classify_']
        # pool_idx = tf.cast(tf.argmax(tf.cast(tf.equal(X[:, :, 0], clf_token), tf.float32), 1), tf.int32)
        # seq_feats = tf.reshape(h, shape=initial_shape[:-1] + [config.n_embed_featurizer])

        # Convolutional Layer (this is all the same layer, just different filter sizes)
        pool_layers = []
        conv_layers = []
        for i, kernel_size in enumerate(config.kernel_sizes):
            conv = tf.layers.conv1d(
                inputs=h,
                filters=config.num_filters_per_size,
                kernel_size=kernel_size,
                padding='same',
                activation=tf.nn.relu,
                name='conv' + str(i)
            )
            conv_layers.append(conv)
            pool = tf.reduce_max(conv, axis=1)
            pool_layers.append(pool)

        # Concat the output of the convolutional layers for use in sequence embedding
        conv_seq = tf.concat(conv_layers, axis=2)
        clf_token = encoder['_classify_']
        pool_idx = tf.cast(tf.argmax(tf.cast(tf.equal(X[:, :, 0], clf_token), tf.float32), 1), tf.int32)
        seq_feats = tf.reshape(conv_seq, shape=initial_shape[:-1] + [config.n_embed])

        # Concatenate the univariate vectors
        clf_h = tf.concat(pool_layers, axis=1)

        # note that, due to convolution and pooling, the dimensionality of the features is much smaller than in the
        # transformer base models
        return {
            'embed_weights': embed_weights,
            'features': clf_h,  # [batch_size, n_embed]
            'sequence_features': seq_feats,  # [batch_size, seq_len, n_embed]
            'pool_idx': pool_idx  # [batch_size]
        }
